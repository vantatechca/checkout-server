"""
WPay Channels integration — hosted card payments via the HPP (Hosted Payment
Page) endpoint.

Flow (hosted redirect, same pattern as pymtz/BTCPay):
  1. POST hpp/request.php  → get a redirect URL to WPay's hosted page
  2. Redirect customer there (they enter card details on WPay's page, not ours)
  3. WPay fires callback_url on completion → /webhooks/wpay
  4. Customer returns to redirect_url

WPay is USD-only (currency is a fixed code, no other option exists) and the
HPP endpoint enforces a $20 minimum order. There's no auth-token step for HPP
(that's only required for the raw 2D/bpay endpoint, which we deliberately do
NOT use — it requires posting raw card data server-side).

WPay's own API is known to sometimes return HTML (with embedded PHP warnings)
instead of clean JSON on error — response parsing below is defensive about
that rather than assuming well-formed JSON always comes back.

Docs: vendor-supplied "Wpay Channels BPAY_API.pdf" + WPay HPP Postman collection.
"""
import json
import logging
import re

import httpx

from config import settings

logger = logging.getLogger(__name__)

HPP_URL    = "https://www.wpaychannels.com/api/cc/hpp/request.php"
STATUS_URL = "https://backoffice.wpaychannels.com/status_check.php"
FETCH_URL  = "https://backoffice.wpaychannels.com/fetch_record.php"

MIN_AMOUNT   = 20.0   # WPay's documented HPP minimum order total (USD)
CURRENCY_USD = "2"    # WPay's fixed currency code — no other value exists


class WPayError(Exception):
    pass


class WPayClient:
    def __init__(self, uid: str | None = None, user_token: str | None = None):
        self.uid        = uid        or settings.WPAY_UID
        self.user_token = user_token or settings.WPAY_USER_TOKEN

    async def submit_hpp_payment(
        self,
        *,
        transaction_id: str,
        amount:         float,
        email:          str,
        first_name:     str,
        last_name:      str,
        phone:          str,
        country:        str,
        state:          str,
        city:           str,
        address:        str,
        zip_code:       str,
        ip_address:     str,
        callback_url:   str,
        redirect_url:   str,
    ) -> str:
        """
        POSTs to WPay's HPP endpoint. Returns the URL to redirect the
        customer to. Raises WPayError if WPay didn't return a usable one.
        """
        if not self.uid:
            raise WPayError("WPAY_UID not configured")

        body = {
            "uid":            self.uid,
            "transaction_id": transaction_id,
            "amount":         f"{round(float(amount), 2):.2f}",
            "currency":       CURRENCY_USD,
            "email":          email,
            "first_name":     first_name,
            "last_name":      last_name,
            "phoneNum":       phone,
            "country":        country,
            "state":          state,
            "city":           city,
            "address":        address,
            "zip":            zip_code,
            "ipAddress":      ip_address,
            "callback_url":   callback_url,
            "redirect_url":   redirect_url,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(HPP_URL, data=body)

        if resp.status_code >= 400:
            raise WPayError(f"WPay HPP request failed (HTTP {resp.status_code}): {resp.text[:500]}")

        redirect = _extract_redirect_url(resp.text)
        if not redirect:
            raise WPayError(f"WPay HPP response had no usable redirect URL: {resp.text[:500]}")
        return redirect

    async def status_check(self, transaction_id: str) -> dict:
        body = {"uid": self.uid, "transaction_id": transaction_id}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(STATUS_URL, data=body)
        data = _parse_json(resp.text)
        if data is None:
            raise WPayError(f"WPay status_check returned non-JSON: {resp.text[:300]}")
        return data

    async def fetch_record(self, transaction_id: str) -> dict:
        body = {"uid": self.uid, "client_transaction_id": transaction_id}
        if self.user_token:
            body["user_token"] = self.user_token
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(FETCH_URL, data=body)
        data = _parse_json(resp.text)
        if data is None:
            raise WPayError(f"WPay fetch_record returned non-JSON: {resp.text[:300]}")
        return data

    async def verify_transaction(self, transaction_id: str) -> dict:
        """status_check first, falls back to fetch_record on failure."""
        try:
            return await self.status_check(transaction_id)
        except Exception as e:
            logger.warning(f"[wpay] status_check failed for {transaction_id}, trying fetch_record: {e}")
            return await self.fetch_record(transaction_id)


def normalize_status(raw: str) -> str:
    """Map WPay's assorted status strings to success|declined|pending."""
    s = (raw or "").strip().lower()
    if s in ("success", "successful", "approved", "paid", "complete", "completed"):
        return "success"
    if s in ("declined", "failed", "fail", "error", "rejected", "denied"):
        return "declined"
    return "pending"


def _parse_json(body: str) -> dict | None:
    """Try straight JSON first, then scrape a JSON object out of a body that
    may have HTML/PHP warnings mixed in (documented WPay behavior)."""
    if not body:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except Exception:
        pass

    match = re.search(r'\{\s*"(?:status|message|acs_url|client_transaction_id)[^}]+\}', body, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _extract_redirect_url(body: str) -> str | None:
    """Pull a usable redirect URL out of WPay's HPP response, which may be
    clean JSON ({"acs_url"/"success_url"/"redirect_url": ...}), or raw HTML/
    a bare URL string."""
    data = _parse_json(body)
    if data:
        for key in ("acs_url", "success_url", "redirect_url", "redirectUrl", "url"):
            val = data.get(key)
            if val and isinstance(val, str) and _is_wpay_url(val):
                return val

    match = re.search(r'https?://[^\s"\'<>]+', body or "")
    if match and _is_wpay_url(match.group(0)):
        return match.group(0)

    return None


def _is_wpay_url(url: str) -> bool:
    return "wpaychannels.com" in url.lower()
