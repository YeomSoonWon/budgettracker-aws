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
          │  Amazon DynamoDB             │      │  AWS KMS (CMK)        │
          │  single table  PK / SK       │◀────▶│  encryption at rest   │
          └──────────────┬──────────────┘      └──────────────────────┘
                         │ monthly (EventBridge)
                         ▼
          ┌─────────────────────────────┐
          │  Amazon S3 (KMS)  backups    │  JSON snapshot per household/month
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
- **KMS customer-managed key** encrypts DynamoDB and S3 at rest, with key rotation.
- **API throttling** on the HTTP API; **no secrets in code** (`.gitignore` blocks
  credentials and `GoogleService-Info.plist`; config comes from env / CDK outputs).
- **Point-in-time recovery** on DynamoDB.

## Deploy (staged)

Prerequisites: an AWS account + configured CLI credentials, Node (for the CDK CLI),
Python 3.12.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npx cdk bootstrap            # one-time per account/region

# 1) minimal: DynamoDB + Lambda + HTTP API (open — smoke test only)
npx cdk deploy

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

Designed to sit inside the AWS Free Tier (on-demand DynamoDB, 128 MB Lambda, HTTP
API, a few KB in S3). Tear everything down with:

```bash
npx cdk destroy
```

## Résumé line

> Built a hybrid local-first sync backend for a personal macOS finance app (SwiftUI),
> replacing Firebase with a self-managed AWS stack (API Gateway, Lambda/Python,
> DynamoDB, Cognito, KMS, S3), defined entirely as code with AWS CDK.
