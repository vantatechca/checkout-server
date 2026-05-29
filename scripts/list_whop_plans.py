"""
scripts/list_whop_plans.py

Lists all plans under your Whop company (sandbox or production based on
WHOP_SANDBOX in .env). Use this to grab plan_xxx IDs without hunting in
the dashboard UI.

Usage:
    # Make sure .env has the right WHOP_SANDBOX value
    python scripts/list_whop_plans.py
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from config import settings


def main() -> int:
    sandbox = bool(getattr(settings, "WHOP_SANDBOX", False))

    if sandbox:
        api_key    = settings.WHOP_SANDBOX_API_KEY
        company_id = settings.WHOP_SANDBOX_COMPANY_ID
        product_id = getattr(settings, "WHOP_SANDBOX_PRODUCT_ID", "")
        api_base   = "https://sandbox-api.whop.com/api/v1"
        env_label  = "SANDBOX"
    else:
        api_key    = settings.WHOP_API_KEY
        company_id = settings.WHOP_COMPANY_ID
        product_id = getattr(settings, "WHOP_PRODUCT_ID", "")
        api_base   = "https://api.whop.com/api/v1"
        env_label  = "PRODUCTION"

    if not api_key or not company_id:
        print(f"ERROR: Missing credentials for {env_label} in .env")
        return 1

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
    }

    # Try a few common list-plans endpoints. Whop has rotated these over the
    # versions, so we attempt the most likely ones in order.
    candidates = [
        f"{api_base}/plans?company_id={company_id}",
        f"{api_base}/companies/{company_id}/plans",
    ]
    if product_id:
        candidates.append(f"{api_base}/products/{product_id}/plans")
        candidates.append(f"{api_base}/plans?product_id={product_id}")

    print(f"Listing plans on {env_label} for company {company_id}")
    if product_id:
        print(f"Product: {product_id}")
    print()

    data = None
    used_url = None
    for url in candidates:
        try:
            resp = httpx.get(url, headers=headers, timeout=15.0)
        except httpx.RequestError as e:
            print(f"  {url} → request error: {e}")
            continue
        if resp.status_code == 200:
            try:
                data = resp.json()
                used_url = url
                break
            except Exception:
                continue
        else:
            print(f"  {url} → HTTP {resp.status_code}")

    if data is None:
        print()
        print("Could not find a working list-plans endpoint. Whop's API version")
        print("may have changed. Try Option A (URL bar trick) instead, or")
        print("contact me with the API errors above.")
        return 1

    print(f"OK: fetched from {used_url}")
    print()

    # Whop responses can be either {"data": [...]} or just [...]
    plans = data.get("data") if isinstance(data, dict) else data
    if not isinstance(plans, list):
        # Single object fallback
        plans = [data]

    # Filter to one-time plans matching the configured currency. Mixing
    # currencies in WHOP_TIER_PLANS would cause customers to be charged
    # the wrong amount (e.g. USD$199 instead of CAD$199), so we exclude
    # any plans whose currency doesn't match WHOP_CURRENCY.
    want_currency = (settings.WHOP_CURRENCY or "cad").lower()
    one_time = []
    skipped_wrong_currency = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        if p.get("plan_type") != "one_time" and p.get("billing_period") not in (None, 0, "one_time"):
            continue
        plan_currency = (p.get("currency") or "").lower()
        price = p.get("initial_price") or p.get("price") or 0
        try:
            price = float(price)
        except (ValueError, TypeError):
            price = 0
        if plan_currency and plan_currency != want_currency:
            skipped_wrong_currency.append((price, plan_currency, p))
            continue
        one_time.append((price, p))

    one_time.sort(key=lambda x: x[0])

    if skipped_wrong_currency:
        print(f"⚠️  Skipped {len(skipped_wrong_currency)} plan(s) with wrong currency (want={want_currency.upper()}):")
        for price, cur, p in sorted(skipped_wrong_currency):
            print(f"     {cur.upper()} {price:>7.2f}  {p.get('id', '?')}  ← delete or ignore")
        print()

    if not one_time:
        print("No one-time plans found.")
        print("Raw response (first 800 chars):")
        print(json.dumps(data, indent=2)[:800])
        return 0

    # Pretty-print
    print(f"Found {len(one_time)} one-time plan(s):")
    print()
    print(f"  {'Price':>10}  {'Plan ID':<25}  {'Title':<30}")
    print(f"  {'─'*10}  {'─'*25}  {'─'*30}")
    for price, p in one_time:
        pid = p.get("id", "?")
        title = (p.get("title") or p.get("name") or "")[:30]
        cur = (p.get("currency") or "").upper()
        print(f"  {cur} {price:>7.2f}  {pid:<25}  {title:<30}")

    # Build the .env line for them
    print()
    print("──────────────────────────────────────────────────")
    print("Paste this into .env:")
    print()
    parts = [f"{int(price) if price == int(price) else price}:{p.get('id')}" for price, p in one_time if p.get("id")]
    env_key = "WHOP_SANDBOX_TIER_PLANS" if sandbox else "WHOP_TIER_PLANS"
    print(f"{env_key}={','.join(parts)}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
