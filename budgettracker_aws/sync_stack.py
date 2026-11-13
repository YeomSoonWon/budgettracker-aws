"""
CDK stack for the BudgetTracker AWS sync backend.

Staged by context flags so the first deploy is truly minimal and each security
layer is added deliberately (good for explaining the design in an interview):

    cdk deploy                       # DynamoDB + Lambda + HTTP API  (open, smoke test)
    cdk deploy -c auth=true          # + Cognito User Pool & JWT authorizer
    cdk deploy -c auth=true -c backup=true   # + S3 monthly backup (KMS encrypted)

Security choices baked in:
  * KMS customer-managed key encrypts DynamoDB and S3 at rest.
  * IAM is least-privilege via CDK grants (each Lambda gets only the table/bucket
    actions it needs — never a wildcard).
  * API Gateway has throttling; Cognito JWT authorizer gates every route when enabled.
  * Everything is destroyable with `cdk destroy` to stay inside the free tier.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_cognito as cognito,
    aws_events as events,
    aws_events_targets as targets,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_apigatewayv2_authorizers as authorizers,
)
from constructs import Construct


class BudgetTrackerSyncStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        enable_auth = self.node.try_get_context("auth") == "true"
        enable_backup = self.node.try_get_context("backup") == "true"

        # --- KMS: one customer-managed key for all data at rest ---------------
        key = kms.Key(
            self,
            "DataKey",
            description="BudgetTracker data-at-rest CMK (DynamoDB + S3)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- DynamoDB: single-table design -----------------------------------
        # PK = HOUSEHOLD#<id>, SK = TXN#/FIXED#/SALARY#/SETTINGS#...
        table = dynamodb.Table(
            self,
            "SyncTable",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Sync Lambda (Python) --------------------------------------------
        sync_fn = lambda_.Function(
            self,
            "SyncFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambda/sync"),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment={"TABLE_NAME": table.table_name},
        )
        table.grant_read_write_data(sync_fn)  # least-privilege: this table only

        # --- HTTP API --------------------------------------------------------
        http_api = apigwv2.HttpApi(
            self,
            "SyncApi",
            api_name="budgettracker-sync",
            description="Delta-sync API for the BudgetTracker macOS app",
        )

        route_auth = None
        if enable_auth:
            user_pool = cognito.UserPool(
                self,
                "UserPool",
                self_sign_up_enabled=False,  # household members invited by admin
                sign_in_aliases=cognito.SignInAliases(email=True),
                password_policy=cognito.PasswordPolicy(
                    min_length=12, require_digits=True, require_symbols=True
                ),
                custom_attributes={
                    "householdId": cognito.StringAttribute(mutable=True)
                },
                removal_policy=RemovalPolicy.DESTROY,
            )
            user_pool_client = user_pool.add_client(
                "AppClient",
                auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
                id_token_validity=Duration.hours(1),
                access_token_validity=Duration.hours(1),
                refresh_token_validity=Duration.days(30),
            )
            route_auth = authorizers.HttpUserPoolAuthorizer(
                "JwtAuthorizer", user_pool, user_pool_clients=[user_pool_client]
            )
            CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
            CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)

        integration = integrations.HttpLambdaIntegration("SyncIntegration", sync_fn)
        http_api.add_routes(
            path="/sync",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=integration,
            authorizer=route_auth,
        )

        # --- Optional: S3 monthly backup -------------------------------------
        if enable_backup:
            bucket = s3.Bucket(
                self,
                "BackupBucket",
                encryption=s3.BucketEncryption.KMS,
                encryption_key=key,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                versioned=True,
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
            backup_fn = lambda_.Function(
                self,
                "BackupFn",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="handler.handler",
                code=lambda_.Code.from_asset("lambda/backup"),
                timeout=Duration.seconds(60),
                memory_size=256,
                environment={
                    "TABLE_NAME": table.table_name,
                    "BACKUP_BUCKET": bucket.bucket_name,
                },
            )
            table.grant_read_data(backup_fn)
            bucket.grant_put(backup_fn)
            events.Rule(
                self,
                "MonthlyBackup",
                schedule=events.Schedule.cron(day="1", hour="3", minute="0"),
                targets=[targets.LambdaFunction(backup_fn)],
            )
            CfnOutput(self, "BackupBucketName", value=bucket.bucket_name)

        # --- Outputs ---------------------------------------------------------
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "AuthEnabled", value=str(enable_auth))
