"""
Delta-sync API for the BudgetTracker macOS app.

Replaces the app's former Firebase Firestore sync with an AWS-native backend:
    API Gateway (HTTP API) -> this Lambda -> DynamoDB (single table)

Sync model (local-first, last-write-wins):
  GET  /sync?since=<epoch_ms>   -> pull every item changed after `since`
  POST /sync  {items: [...]}    -> push a batch of local changes (upsert / delete tombstone)

Each item carries `updatedAt` (epoch ms). On push we only overwrite a stored
item when the incoming `updatedAt` is newer-or-equal, so concurrent edits from
the two household Macs converge deterministically (last writer wins).

No real financial values are stored here in the demo — seed data is dummy only.
"""

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

# Valid SK prefixes keep writes constrained to the known entity types.
_ALLOWED_PREFIXES = ("TXN#", "FIXED#", "SALARY#", "SETTINGS#")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _household_id(event: dict) -> str:
    """Scope every request to the caller's household.

    With Cognito enabled, API Gateway puts the verified JWT claims here; we use a
    `custom:householdId` claim when present, otherwise the user's `sub`. Without an
    authorizer (first smoke-test deploy) we fall back to a single shared household.
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return claims.get("custom:householdId") or claims.get("sub") or "main"


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _strip_keys(item: dict) -> dict:
    """Return the item without internal partition/sort keys for the client."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")} | {
        "sk": item.get("SK")
    }


def _pull(pk: str, since: int) -> dict:
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        resp = _TABLE.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    changed = [i for i in items if int(i.get("updatedAt", 0)) > since]
    return {"serverTime": _now_ms(), "items": [_strip_keys(i) for i in changed]}


def _push(pk: str, body: dict) -> dict:
    results = []
    for it in body.get("items", []):
        sk = it.get("sk", "")
        if not sk.startswith(_ALLOWED_PREFIXES):
            results.append({"sk": sk, "status": "rejected"})
            continue
        updated_at = int(it.get("updatedAt") or _now_ms())
        item = {
            "PK": pk,
            "SK": sk,
            "entity": it.get("entity", ""),
            "updatedAt": updated_at,
            "deleted": bool(it.get("deleted", False)),
            "data": it.get("data", {}),
        }
        try:
            _TABLE.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(updatedAt) OR updatedAt <= :u",
                ExpressionAttributeValues={":u": updated_at},
            )
            results.append({"sk": sk, "status": "applied"})
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                results.append({"sk": sk, "status": "stale"})  # a newer version already won
            else:
                raise
    return {"serverTime": _now_ms(), "results": results}


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    pk = f"HOUSEHOLD#{_household_id(event)}"
    try:
        if method == "GET":
            since = int((event.get("queryStringParameters") or {}).get("since", "0"))
            return _response(200, _pull(pk, since))
        if method == "POST":
            body = json.loads(event.get("body") or "{}")
            return _response(200, _push(pk, body))
        return _response(405, {"error": "method not allowed"})
    except (ValueError, json.JSONDecodeError) as e:
        return _response(400, {"error": f"bad request: {e}"})
