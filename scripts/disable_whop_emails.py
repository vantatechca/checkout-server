"""
scripts/disable_whop_emails.py

One-time setup script: turns OFF Whop's transactional email sending for the
company. After running this, Whop will no longer send receipts, refund
notifications, or dispute notifications to your customers.

Reads credentials from .env. Picks sandbox or production based on the
WHOP_SANDBOX flag — run it once per environment.

Usage:
    # First set WHOP_SANDBOX=true in .env, then:
    python scripts/disable_whop_emails.py

    # Then set WHOP_SANDBOX=false, then:
    python scripts/disable_whop_emails.py

Re-running is safe — the API is idempotent.
"""

import sys
import json
from pathlib import Path

# Add parent dir to path so we can import config (script lives in scripts/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from config import settings


def main() -> int:
    sandbox = bool(getattr(settings, "WHOP_SANDBOX", False))

    if sandbox:
        api_key    = settings.WHOP_SANDBOX_API_KEY
        company_id = settings.WHOP_SANDBOX_COMPANY_ID
        api_base   = "https://sandbox-api.whop.com/api/v1"
        env_label  = "SANDBOX"
    else:
        api_key    = settings.WHOP_API_KEY
        company_id = settings.WHOP_COMPANY_ID
        api_base   = "https://api.whop.com/api/v1"
        env_label  = "PRODUCTION"

    if not api_key or not company_id:
        print(f"ERROR: Missing credentials for {env_label} in .env")
        print(f"  Need: WHOP_{'SANDBOX_' if sandbox else ''}API_KEY")
        print(f"  Need: WHOP_{'SANDBOX_' if sandbox else ''}COMPANY_ID")
        return 1

    url = f"{api_base}/companies/{company_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    body = {"send_customer_emails": False}

    print(f"Disabling customer emails on {env_label} company {company_id}")
    print(f"Endpoint: POST {url}")
    print(f"Body: {json.dumps(body)}")
    print()

    # Whop's update-company endpoint uses PATCH (per their OpenAPI spec at
    # https://docs.whop.com/api-reference/companies/update-company.md).
    # We still try POST/PUT as fallbacks in case the API changes again, and
    # we retry past 404/405 because "wrong method on this path" returns 404
    # on Whop's API rather than 405.
    last_response = None
    for method in ("PATCH", "POST", "PUT"):
        try:
            resp = httpx.request(method, url, json=body, headers=headers, timeout=15.0)
        except httpx.RequestError as e:
            print(f"ERROR: request failed: {e}")
            return 1

        last_response = resp
        print(f"  {method} → HTTP {resp.status_code}")

        if resp.status_code in (404, 405):
            # Try the next method
            continue

        if resp.status_code >= 400:
            print(f"  Response body: {resp.text[:500]}")
            if resp.status_code == 401:
                print("  → Auth failed. Check your API key.")
            elif resp.status_code == 422:
                print("  → Field rejected. Whop may have changed the field name.")
            return 1

        # Success — try to confirm the change took effect
        try:
            data = resp.json()
            current = data.get("send_customer_emails")
            if current is False:
                print()
                print(f"✅ SUCCESS: send_customer_emails is now FALSE on {env_label}")
                print(f"   Future {env_label.lower()} transactions will NOT email customers from Whop.")
                return 0
            else:
                print(f"⚠️  Request succeeded but send_customer_emails = {current!r} (expected False)")
                print(f"   Response: {json.dumps(data, indent=2)[:800]}")
                return 1
        except Exception:
            print(f"⚠️  Request succeeded (HTTP {resp.status_code}) but response wasn't JSON:")
            print(f"   {resp.text[:300]}")
            return 0

    print()
    print("ERROR: All HTTP methods returned 404/405 — endpoint not found.")
    if last_response is not None:
        print(f"   Last response body: {last_response.text[:500]}")
    print("   Whop's API may have changed paths. Possible fallback paths to try:")
    print(f"     - https://{'sandbox-api' if sandbox else 'api'}.whop.com/api/v5/companies/{company_id}")
    print(f"     - https://{'sandbox-api' if sandbox else 'api'}.whop.com/api/v2/companies/{company_id}")
    print("   Or contact Whop support to disable customer emails for this company manually.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
