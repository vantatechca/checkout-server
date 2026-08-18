"""
pymtz integration — Credit card payments via hosted payment page.

Flow (hosted redirect, same pattern as NowPayments):
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


def _pymtz_key_for(country: str | None) -> str:
    """Pick the right pymtz API key for a given country (CA / US).
    Falls back to the legacy single key if the per-country one is unset."""
    c = (country or "").upper()
    legacy = getattr(settings, "PYMTZ_API_KEY", "") or ""
    if c == "US":
        return getattr(settings, "PYMTZ_API_KEY_US", "") or legacy
    if c == "CA":
        return getattr(settings, "PYMTZ_API_KEY_CA", "") or legacy
    return legacy


class PymtzClient:
    def __init__(self, country: str | None = None):
        self.country = (country or "").upper() or None
        self.api_key = _pymtz_key_for(self.country)
        # One-line visibility: tells you exactly which key is in use per request.
        # Logs the prefix only — never the full secret.
        legacy = getattr(settings, "PYMTZ_API_KEY", "") or ""
        which = (
            "CA" if self.country == "CA" and getattr(settings, "PYMTZ_API_KEY_CA", "") else
            "US" if self.country == "US" and getattr(settings, "PYMTZ_API_KEY_US", "") else
            "LEGACY"
        )
        key_tail = self.api_key[-6:] if self.api_key else "<empty>"
        logger.info(f"[pymtz] country={self.country} → using {which} key (...{key_tail})")

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
        # Customer / billing prefill fields — pymtz docs don't list these but
        # the upstream processor (WiPay) accepts them, and unknown fields are
        # ignored. Sent in multiple common shapes so whichever the hosted
        # form reads gets populated.
        first_name:  str = "",
        last_name:   str = "",
        phone:       str = "",
        address1:    str = "",
        address2:    str = "",
        city:        str = "",
        state:       str = "",   # province / state — 2-letter code if possible
        postal_code: str = "",
        country:     str = "",   # 2-letter ISO ("CA", "US")
    ) -> dict:
        """
        Creates a pymtz payment intent. Returns the full response which
        includes `id` (pay_...) and `payment_url`.
        """
        if not self.api_key:
            raise PymtzError("PYMTZ_API_KEY not configured")

        full_name = (f"{first_name} {last_name}").strip()
        country_u = (country or "").upper()
        state_u   = (state   or "").upper()

        customer = {
            "email":      email,
            "first_name": first_name,
            "last_name":  last_name,
            "name":       full_name,
            "phone":      phone,
        }
        billing = {
            "line1":       address1,
            "line2":       address2,
            "address1":    address1,
            "address2":    address2,
            "street":      address1,
            "city":        city,
            "state":       state_u,
            "province":    state_u,
            "region":      state_u,
            "postal_code": postal_code,
            "zip":         postal_code,
            "country":     country_u,
        }
        # Strip empties so we don't ship a wall of "" — keeps the request body
        # small and avoids any provider that treats "" as "explicitly cleared".
        customer = {k: v for k, v in customer.items() if v}
        billing  = {k: v for k, v in billing.items()  if v}

        body = {
            "amount":      round(float(amount), 2),
            "currency":    currency.upper(),
            "description": description or f"Order {order_id}",
            "return_url":  return_url,
            "cancel_url":  cancel_url,
            "email":       email,
            "metadata": {
                "order_id":   order_id,
                "first_name": first_name,
                "last_name":  last_name,
                "phone":      phone,
                "address1":   address1,
                "address2":   address2,
                "city":       city,
                "state":      state_u,
                "postal_code": postal_code,
                "country":    country_u,
                **(metadata or {}),
            },
        }
        # Top-level customer fields (common naming conventions)
        if first_name:  body["first_name"]  = first_name
        if last_name:   body["last_name"]   = last_name
        if full_name:   body["name"]        = full_name
        if phone:       body["phone"]       = phone
        if address1:    body["address1"]    = address1
        if address2:    body["address2"]    = address2
        if city:        body["city"]        = city
        if state_u:     body["state"]       = state_u
        if postal_code:
            body["postal_code"] = postal_code
            body["zip"]         = postal_code
        if country_u:   body["country"]     = country_u
        # Nested objects (Stripe / Square / generic style)
        if customer:    body["customer"]        = customer
        if billing:
            body["billing"]         = billing
            body["billing_address"] = billing

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
    Verify the webhook signature against ANY configured secret (CA, US, or
    legacy). With two pymtz accounts the webhook URL can be shared — we just
    accept the call if the signature matches any of the secrets we know.
    pymtz signs the raw body with HMAC-SHA256. If no secret is configured we
    fail-open ONLY in non-production to ease local testing.
    """
    secrets = [
        getattr(settings, "PYMTZ_WEBHOOK_SECRET_CA", "") or "",
        getattr(settings, "PYMTZ_WEBHOOK_SECRET_US", "") or "",
        getattr(settings, "PYMTZ_WEBHOOK_SECRET", "")    or "",
    ]
    secrets = [s for s in secrets if s]
    if not secrets:
        # No secret set — accept in dev, reject in production.
        return settings.ENVIRONMENT != "production"
    if not sig_header:
        return False
    # Some providers prefix with "sha256=" — strip if present
    got = sig_header.split("=", 1)[-1] if "=" in sig_header else sig_header
    for secret in secrets:
        try:
            expected = hmac.new(
                secret.encode(),
                payload_bytes,
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(expected, got):
                return True
        except Exception:
            continue
    return False


# pymtz status → our PaymentStatus value
PYMTZ_STATUS_MAP = {
    "pending":   "pending",
    "completed": "paid",
    "failed":    "failed",
    "expired":   "expired",
    "refunded":  "refunded",   # set by the refund.created webhook
}