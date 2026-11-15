# BudgetTracker AWS Sync Backend

A local-first macOS finance app ([BudgetTracker](https://github.com/YeomSoonWon/BudgetTracker),
SwiftUI) synced to a self-managed **AWS** backend, with all infrastructure defined
as code in **AWS CDK (Python)**.

The app originally synced through Firebase Firestore (a managed BaaS). This project
replaces that with an AWS-native stack the owner designs and operates directly —
demonstrating **cloud ↔ on-premises (local app) integration** and **Infrastructure
as Code**.

> All data in this repository and in any demo is **dummy only**. Real household
> financial figures are never committed, deployed, or screenshotted.

## Architecture

```
          ┌─────────────────────────────┐
          │   macOS SwiftUI app          │  local-first: works offline,
          │   (on-premises / local DB)   │  syncs only changed records
          └──────────────┬──────────────┘
                         │  HTTPS + Cognito JWT
                         │  GET /sync?since=<ts>   (pull deltas)
                         │  POST /sync {items:[…]} (push batch, last-write-wins)
                         ▼
          ┌─────────────────────────────┐      ┌──────────────────────┐
          │  Amazon API Gateway (HTTP)   │─────▶│  Amazon Cognito      │
          │  throttling + JWT authorizer │ auth │  User Pool           │
          └──────────────┬──────────────┘      └──────────────────────┘
                         │ (IAM least-privilege)
                         ▼
          ┌─────────────────────────────┐
          │  AWS Lambda  (Python 3.12)   │  delta-sync logic
          └──────────────┬──────────────┘
                         ▼
          ┌─────────────────────────────┐      ┌──────────────────────┐
          │  Amazon DynamoDB             │  provisioned 1 RCU / 1 WCU,
          │  single table  PK / SK       │  AWS-owned key encryption
          └──────────────┬──────────────┘
                         │ monthly (EventBridge)
                         ▼
          ┌─────────────────────────────┐
          │  Amazon S3 (SSE-S3) backups  │  JSON snapshot per household/month
          └─────────────────────────────┘
```

### Data model → single-table design

The app's former Firestore structure (`households/main` + 4 subcollections) maps
onto one DynamoDB table:

| Entity | Firestore path | DynamoDB `PK` / `SK` |
|---|---|---|
| Transaction | `…/transactions/{id}` | `HOUSEHOLD#<id>` / `TXN#<uuid>` |
| FixedExpense | `…/fixedExpenses/{id}` | `HOUSEHOLD#<id>` / `FIXED#<uuid>` |
| MonthlySalary | `…/salaries/{y}-{m}` | `HOUSEHOLD#<id>` / `SALARY#<y>-<m>` |
| BudgetSettings | `…/settings/default` | `HOUSEHOLD#<id>` / `SETTINGS#default` |

Every item also carries `updatedAt` (epoch ms), `deleted` (tombstone), and the
original fields under `data`.

## Design decisions

- **Local-first + delta sync.** The app stays fully usable offline; only records
  changed since the last `updatedAt` are exchanged. This is the on-prem ↔ cloud
  hybrid pattern, and keeps payloads tiny (free-tier friendly).
- **Last-write-wins conflict resolution.** The two household Macs can edit
  concurrently; a conditional `PutItem` (`updatedAt <= incoming`) makes the newest
  write win deterministically — simple and correct for a shared personal ledger.
- **Single-table DynamoDB.** One partition per household keeps a full pull to a
  single `Query`, no joins, no hot keys.
- **Python everywhere** (Lambda + CDK) to match the consultant role's language
  requirement and keep the stack coherent.
- **Staged deploy** via context flags, so each security layer is added — and can be
  explained — on its own.

## Security

- **Cognito** User Pool + JWT authorizer gates every API route (replacing the old
  no-auth shared Firestore document). Strong password policy, 1-hour tokens.
- **IAM least privilege** — each Lambda is granted only the specific table/bucket
  actions it needs (CDK `grant_*`), never a wildcard.
- **Encryption at rest** with AWS-owned keys (DynamoDB) and SSE-S3 (S3), plus
  TLS-only access to the backup bucket. A KMS customer-managed key was dropped on
  purpose: it costs about $1/month and the project runs at $0.
- **API throttling** (5 req/s, burst 10) on the HTTP API; **no secrets in code** (`.gitignore` blocks
  credentials and `GoogleService-Info.plist`; config comes from env / CDK outputs).
- **Cost guardrail** — a zero-spend AWS Budget (created when `-c alertEmail=…` is
  passed) emails on ~$0.01 of actual spend or any forecast over the $1 budget, an
  early warning against a leaked-key bill. The email is supplied at deploy time and
  never committed.
- **Automatic lockout** — alerts only notify, so a Budget Action (`APPLY_IAM_POLICY`,
  automatic approval) attaches a deny-all policy to the `guardUser` IAM user once actual
  monthly cost passes `guardLimitUsd`. A stolen access key for that user stops working.
  The Budgets execution role can only attach/detach *that* policy on *that* user.
  Recover by signing in as root and resetting the action in the Budgets console.

## Deploy (staged)

Prerequisites: an AWS account + configured CLI credentials, Node (for the CDK CLI),
Python 3.12.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npx cdk bootstrap            # one-time per account/region

# 1) minimal: DynamoDB + Lambda + HTTP API (open — smoke test only)
#    -c alertEmail  -> zero-spend budget e-mail alerts
#    -c guardUser   -> auto-lockout of that IAM user when spend passes guardLimitUsd (default 1)
npx cdk deploy -c alertEmail=you@example.com -c guardUser=my-iam-user -c guardLimitUsd=1

# 2) add Cognito auth
npx cdk deploy -c auth=true

# 3) add S3 monthly backup
npx cdk deploy -c auth=true -c backup=true
```

Seed and verify with dummy data (prints the push result and pulls it back):

```bash
python3 scripts/seed_dummy.py <ApiEndpoint>          # open mode
python3 scripts/seed_dummy.py <ApiEndpoint> --token <idToken>   # with auth
```

## Cost & cleanup

Target cost is **$0**. Each choice that would otherwise bill was changed:

| Item | Choice | Why |
|---|---|---|
| Encryption | AWS-owned key / SSE-S3 | a KMS customer-managed key costs ~$1/month |
| DynamoDB | provisioned 1 RCU / 1 WCU | always-free covers 25 provisioned units; on-demand requests are billed |
| DynamoDB PITR | off | billed per GB-month |
| S3 backup | off by default, no versioning | storage would accumulate |
| Lambda logs | 1-week retention, deleted on destroy | avoid growing log storage |
| API | throttled 5 req/s | caps request volume if the URL leaks |
| Budgets | 2 budgets, 1 with an action | the first two action-enabled budgets are free |

Tear everything down with:

```bash
npx cdk destroy
```

Notes:
- `cdk bootstrap` creates a separate `CDKToolkit` stack with a small S3 staging bucket.
  For a full cleanup, empty that bucket and delete the `CDKToolkit` stack in the
  CloudFormation console.
- If the lockout has fired, reset the Budget Action (root, Budgets console) before
  `cdk destroy`; the attached deny policy blocks both the user and the policy deletion.

## Résumé line

> Built a hybrid local-first sync backend for a personal macOS finance app (SwiftUI),
> replacing Firebase with a self-managed AWS stack (API Gateway, Lambda/Python,
> DynamoDB, Cognito, S3), defined entirely as code with AWS CDK and designed to run at
> $0 with budget alerts and an automatic IAM lockout.
