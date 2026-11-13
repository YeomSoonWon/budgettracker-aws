#!/usr/bin/env python3
import aws_cdk as cdk

from budgettracker_aws.sync_stack import BudgetTrackerSyncStack

app = cdk.App()
BudgetTrackerSyncStack(
    app,
    "BudgetTrackerSyncStack",
    description="AWS sync backend for the BudgetTracker macOS app (local-first + cloud sync)",
)
app.synth()
