"""
pymtz integration — Credit card payments via hosted payment page.

Flow (hosted redirect, same pattern as NowPayments/Whop):
  1. POST /api/v1/payments  → create payment intent, get payment_url
  2. Redirect customer to payment_url (they enter card on pymtz's page)
  3. pymtz fires webhook → /webhooks/pymtz on payment.completed
  4. Customer returns to return_url

Docs: https://pymtz.co  (REST API v1)
"""
import hashlib
import hmac
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

PYMTZ_BASE = "https://pymtz.co/api/v1"


class PymtzError(Exception):
    pass


class PymtzClient:
    def __init__(self):
        self.api_key = settings.PYMTZ_API_KEY

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    async def create_payment(
        self,
        *,
        order_id:    str,
        amount:      float,
        currency:    str = "USD",
        description: str = "",
        email:       str = "",
        return_url:  str = "",
        cancel_url:  str = "",
        metadata:    dict | None = None,
    ) -> dict:
        """
        Creates a pymtz payment intent. Returns the full response which
        includes `id` (pay_...) and `payment_url`.
        """
        if not self.api_key:
            raise PymtzError("PYMTZ_API_KEY not configured")

        body = {
            "amount":      round(float(amount), 2),
            "currency":    currency.upper(),
            "description": description or f"Order {order_id}",
            "return_url":  return_url,
            "cancel_url":  cancel_url,
            "email":       email,
            "metadata":    {"order_id": order_id, **(metadata or {})},
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PYMTZ_BASE}/payments",
                headers=self._headers(),
                json=body,
            )
        if resp.status_code not in (200, 201):
            raise PymtzError(f"pymtz payment creation failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        if not data.get("payment_url"):
            raise PymtzError(f"pymtz response missing payment_url: {data}")
        return data

    async def get_payment(self, payment_id: str) -> dict:
        """Fetch current status of a payment by pymtz payment ID (pay_...)."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{PYMTZ_BASE}/payments/{payment_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()


def verify_pymtz_webhook(payload_bytes: bytes, sig_header: str) -> bool:
    """
    Verify the webhook signature. pymtz signs the raw body with the webhook
    secret (whsec_...) using HMAC-SHA256. If no secret is configured we
    fail-open ONLY in non-production to ease local testing.
    """
    secret = getattr(settings, "PYMTZ_WEBHOOK_SECRET", "") or ""
    if not secret:
        # No secret set — accept in dev, reject in production.
        return settings.ENVIRONMENT != "production"
    if not sig_header:
        return False
    try:
        expected = hmac.new(
            secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        # Some providers prefix with "sha256=" — strip if present
        got = sig_header.split("=", 1)[-1] if "=" in sig_header else sig_header
        return hmac.compare_digest(expected, got)
    except Exception:
        return False


# pymtz status → our PaymentStatus value
PYMTZ_STATUS_MAP = {
    "pending":   "pending",
    "completed": "paid",
    "failed":    "failed",
    "expired":   "expired",
}