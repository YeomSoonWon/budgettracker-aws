#!/usr/bin/env python3
"""
Seed the sync API with DUMMY data and verify a pull round-trip.

Usage:
    python3 scripts/seed_dummy.py <api_endpoint> [--token <cognito_id_token>]

    <api_endpoint>  e.g. https://abc123.execute-api.ap-northeast-2.amazonaws.com

All amounts here are fictional. Real household financial figures must never be
sent to the cloud, committed, or screenshotted (project constraint).
"""

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone

DUMMY_ITEMS = [
    {
        "sk": "SETTINGS#default",
        "entity": "settings",
        "data": {
            "savingsPerPerson": 1000000,
            "sunwonAllowance": 300000,
            "jiyoungAllowance": 300000,
            "livingExpense": 1200000,
        },
    },
    {
        "sk": "SALARY#2026-05",
        "entity": "salary",
        "data": {"year": 2026, "month": 5, "sunwonAmount": 3200000, "jiyoungAmount": 2800000},
    },
    {
        "sk": "FIXED#11111111-1111-1111-1111-111111111111",
        "entity": "fixedExpense",
        "data": {"name": "월세(더미)", "amount": 700000, "iconName": "house.fill",
                 "isActive": True, "sortOrder": 0},
    },
    {
        "sk": "TXN#22222222-2222-2222-2222-222222222222",
        "entity": "transaction",
        "data": {"title": "장보기(더미)", "amount": 54000, "typeRaw": "지출",
                 "categoryRaw": "식비", "date": "2026-05-03T09:00:00Z",
                 "note": "", "pocket": "생활비카드"},
    },
    {
        "sk": "TXN#33333333-3333-3333-3333-333333333333",
        "entity": "transaction",
        "data": {"title": "급여(더미)", "amount": 3200000, "typeRaw": "수입",
                 "categoryRaw": "급여", "date": "2026-05-25T00:00:00Z",
                 "note": "", "pocket": "월급통장"},
    },
]


def _post(url: str, payload: dict, token: str | None) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("endpoint", help="HTTP API base URL (no trailing slash)")
    ap.add_argument("--token", help="Cognito ID token (when auth is enabled)")
    args = ap.parse_args()

    base = args.endpoint.rstrip("/")
    now = int(time.time() * 1000)
    items = [{**it, "updatedAt": now, "deleted": False} for it in DUMMY_ITEMS]

    print(f"-> POST {base}/sync  ({len(items)} dummy items)")
    push = _post(f"{base}/sync", {"items": items}, args.token)
    print(json.dumps(push, ensure_ascii=False, indent=2))

    print(f"\n-> GET {base}/sync?since=0  (pull everything back)")
    pull = _get(f"{base}/sync?since=0", args.token)
    print(f"   pulled {len(pull.get('items', []))} items; "
          f"serverTime={datetime.fromtimestamp(pull['serverTime']/1000, timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
