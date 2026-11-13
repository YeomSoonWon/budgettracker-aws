"""
Monthly backup Lambda (optional, scheduled by EventBridge).

Scans the sync table and writes a single JSON snapshot to S3 under
    backups/<household>/<YYYY-MM>/snapshot-<epoch_ms>.json

The S3 bucket is encrypted with the same KMS customer-managed key as DynamoDB.
This is a point-in-time export for the user's own records; it holds dummy data
in the demo environment.
"""

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_S3 = boto3.client("s3")
_BUCKET = os.environ["BACKUP_BUCKET"]


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def handler(_event, _context):
    items, kwargs = [], {}
    while True:
        resp = _TABLE.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    # Group by household so each couple's ledger backs up independently.
    by_household: dict[str, list] = {}
    for it in items:
        hh = str(it.get("PK", "HOUSEHOLD#main")).split("#", 1)[-1]
        by_household.setdefault(hh, []).append(it)

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    stamp = int(time.time() * 1000)
    written = []
    for hh, rows in by_household.items():
        key = f"backups/{hh}/{month}/snapshot-{stamp}.json"
        _S3.put_object(
            Bucket=_BUCKET,
            Key=key,
            Body=json.dumps({"household": hh, "items": rows}, cls=_DecimalEncoder),
            ContentType="application/json",
        )
        written.append(key)

    return {"backedUp": written, "itemCount": len(items)}
