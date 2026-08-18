"""
Stripe direct card processor integration.

Its own card processor — customer cards go to whichever processor was
picked on the checkout, each rail settles to its own bank, then funds
manually move to Grey.

Flow:
  1. Customer enters card details in our Stripe Elements modal.
  2. Stripe.js (loaded in the browser) tokenizes the card client-side and
     returns a PaymentMethod ID (`pm_xxx`) — single-use, no card data ever
     touches our server.
  3. Frontend POSTs `payment_method_id` + order info to
     /api/checkout/stripe_direct.
  4. This client creates + confirms a PaymentIntent on Stripe's API.
  5. On success → order marked paid; funds settle to the merchant's Stripe
     payout bank within T+2.
  6. (Optional) Webhook fires later for settlement/refund events.

Cloaking (critical — peptide business):
  * `description` sent to Stripe is neutral: "Order ORD-XXXXX" — NEVER
    contains product names or anything pharma-flavored.
  * `metadata` only has internal IDs (order_id, source_domain) — Stripe
    internal logging only, never customer-visible.
  * `statement_descriptor_suffix` is optional; appended to the merchant
    name on the bank statement. Use neutral text only (e.g. "ORD123456").
    The base merchant name (cloaked, e.g. "ABC RESEARCH LLC") is configured
    in the Stripe dashboard.
  * `receipt_email` is intentionally NOT set — Stripe auto-receipts are
    disabled at the dashboard level. We send our own branded confirmation
    via Resend, so the customer never sees a "Stripe" or processor-named
    email.
  * Line items / product details are NEVER sent to Stripe. They stay in
    our DB and in Shopify only.

Test mode:
  Stripe uses different KEYS for test vs live (sk_test_xxx vs sk_live_xxx).
  Environments are entirely separated by credentials. Set STRIPE_SECRET_KEY
  to a test key to test without real charges. No code change needed to
  flip — just env keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Stripe's API has ONE base URL — sandbox/live distinction lives in the
# API key prefix (sk_test_ vs sk_live_).
STRIPE_API_URL = "https://api.stripe.com/v1"

# Stripe.js script — same URL for test and live (key determines mode).
STRIPE_JS_URL = "https://js.stripe.com/v3/"


class StripeError(Exception):
    """Raised when a Stripe API call fails at the transport or parse level."""
    pass


class StripeDirectClient:
    """
    Thin client over Stripe's PaymentIntents API.

    Uses raw httpx (not the official stripe SDK) — fewer dependencies,
    consistent with our other integrations (onramp_wp, wpay).
    Stripe's API is REST/form-encoded, well-documented, no SDK quirks.
    """

    def __init__(self):
        self.secret_key      = (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
        self.webhook_secret  = (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip()
        # Statement descriptor suffix — appended to the merchant name on
        # the bank statement. The base name (cloaked, e.g. "ABC RESEARCH LLC")
        # is set in the Stripe dashboard. Leave blank to use the base only.
        self.descriptor_suffix = (
            getattr(settings, "STRIPE_STATEMENT_DESCRIPTOR_SUFFIX", "") or ""
        ).strip()

    def configured(self) -> bool:
        # sk_live_ for production, sk_test_ for test. Both are valid.
        return self.secret_key.startswith("sk_")

    def is_live(self) -> bool:
        return self.secret_key.startswith("sk_live_")

    # ── Charge a card via PaymentMethod ID (from Stripe Elements) ──────────
    async def create_and_confirm_payment(
        self,
        *,
        payment_method_id: str,
        amount: float,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        customer_ip: Optional[str] = None,
        source_domain: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        Create + confirm a PaymentIntent in one call.

        Stripe's API takes amount in the SMALLEST CURRENCY UNIT (cents for USD/
        CAD, not dollars). We convert from our dollars-stored amount here.

        Returns a normalized dict:
          {
            "success":           bool,
            "status":            str,  # "succeeded" / "requires_action" / "requires_payment_method" / ...
            "payment_intent_id": str,
            "charge_id":         str,
            "amount":            float,  # back in dollars
            "currency":          str,
            "last4":             str,
            "brand":             str,    # visa / mastercard / etc.
            "next_action":       dict|None,  # 3DS / SCA challenge if needed
            "message":           str,
            "raw":               dict,
          }
        """
        if not self.configured():
            raise StripeError(
                "Stripe not configured — set STRIPE_SECRET_KEY (sk_live_... or sk_test_...) in .env"
            )

        # Stripe wants amount in cents (or smallest unit per currency).
        cents = int(round(float(amount) * 100))
        if cents <= 0:
            raise StripeError(f"Invalid amount {amount} → {cents} cents")

        # Form-encoded body (Stripe's API spec). Use list-of-tuples for
        # nested params like metadata[key].
        params: list[tuple[str, str]] = [
            ("amount",                str(cents)),
            ("currency",              (currency or "usd").lower()),
            ("payment_method",        payment_method_id),
            ("confirm",               "true"),       # create + confirm in one call
            ("confirmation_method",   "automatic"),  # let Stripe handle 3DS routing
            # Cloaking: neutral description (Stripe dashboard view, merchant-only)
            ("description",           (description or f"Order {order_id}")[:500]),
            # Don't auto-email a Stripe receipt — we send our own branded one.
            # Omitting receipt_email accomplishes this. Setting it to "" would
            # be rejected as an invalid email — DON'T pass the field at all.
            #
            # Disable automatic payment methods we don't want (wallets, etc).
            # Card-only for now — simpler surface.
            ("payment_method_types[]", "card"),
            # Required for SCA-regulated regions even if we don't show wallets.
            ("return_url",            (settings.BASE_URL or "https://pepscheckoutportal.com").rstrip("/") + f"/order/{order_id}/confirmation"),
        ]

        # Statement descriptor suffix — appended to the merchant name on the
        # bank statement. Use neutral text (no product/pharma references).
        # Stripe has a 22-char limit on the full descriptor (merchant + suffix);
        # the suffix gets allocated space dynamically based on merchant length.
        if self.descriptor_suffix:
            # Sanitize to alphanumeric + space + a few safe punctuation chars.
            suffix = ''.join(
                c for c in self.descriptor_suffix
                if c.isalnum() or c in (' ', '-', '.', '*')
            )[:22]
            if suffix:
                params.append(("statement_descriptor_suffix", suffix))

        # Internal-only metadata. Customer NEVER sees this; only visible in
        # the Stripe dashboard for the merchant. Cloaking is preserved since
        # we only put neutral identifiers here.
        params.append(("metadata[order_id]",      order_id))
        if source_domain:
            params.append(("metadata[source_domain]", source_domain[:100]))
        if customer_ip:
            params.append(("metadata[client_ip]", customer_ip[:64]))

        headers = {
            "Authorization":       f"Bearer {self.secret_key}",
            "Content-Type":        "application/x-www-form-urlencoded",
            # Idempotency: Stripe's native deduplication. Without this, a
            # double-click can charge the card twice. With it, the second
            # POST returns the same PaymentIntent as the first.
            "Idempotency-Key":     idempotency_key or f"order-{order_id}-{int(time.time() // 60)}",
        }

        url = f"{STRIPE_API_URL}/payment_intents"

        logger.info(f"[stripe_direct] charging ${amount:.2f} {currency.upper()} for order {order_id}")

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(url, data=params, headers=headers)
            except httpx.RequestError as e:
                raise StripeError(f"Network error calling Stripe: {e}")

        try:
            data = resp.json()
        except Exception:
            raise StripeError(f"Failed to parse Stripe response: {resp.text[:300]}")

        return self._normalize_payment_intent(data, resp.status_code)

    def _normalize_payment_intent(self, data: dict, status_code: int) -> dict:
        # Stripe returns error responses with HTTP 4xx and a top-level "error"
        # object. PaymentIntent responses have status_code=200 and the
        # PaymentIntent object directly.
        if status_code >= 400 or data.get("error"):
            err = data.get("error", {}) or {}
            return {
                "success":           False,
                "status":            err.get("code") or "error",
                "payment_intent_id": err.get("payment_intent", {}).get("id", "") if isinstance(err.get("payment_intent"), dict) else "",
                "charge_id":         "",
                "amount":            0.0,
                "currency":          "",
                "last4":             "",
                "brand":             "",
                "next_action":       None,
                "message":           err.get("message") or err.get("type") or "Card payment failed",
                "raw":               data,
            }

        pi = data  # the PaymentIntent object
        status = pi.get("status", "")

        # Pull last4 / brand from the latest_charge if available.
        latest_charge = pi.get("latest_charge")
        charge_obj = {}
        if isinstance(latest_charge, dict):
            charge_obj = latest_charge
        # Or from payment_method.card if expanded:
        pm = pi.get("payment_method") or {}
        if isinstance(pm, dict) and pm.get("card"):
            card = pm["card"]
            last4 = card.get("last4", "")
            brand = card.get("brand", "")
        else:
            payment_details = (charge_obj.get("payment_method_details") or {}).get("card") or {}
            last4 = payment_details.get("last4", "")
            brand = payment_details.get("brand", "")

        return {
            "success":           (status == "succeeded"),
            "status":            status,
            "payment_intent_id": pi.get("id", ""),
            "charge_id":         charge_obj.get("id", "") if isinstance(charge_obj, dict) else "",
            "amount":            float(pi.get("amount", 0)) / 100.0,
            "currency":          pi.get("currency", "").upper(),
            "last4":             last4,
            "brand":             brand,
            # Stripe returns next_action for 3DS/SCA challenges.
            "next_action":       pi.get("next_action"),
            "message":           self._extract_message(pi, status),
            "raw":               data,
        }

    @staticmethod
    def _extract_message(pi: dict, status: str) -> str:
        # Try last_payment_error first (decline reason from the bank)
        err = pi.get("last_payment_error") or {}
        if err.get("message"):
            return err["message"]
        # Fall back to status descriptions
        return {
            "succeeded":              "Payment successful",
            "requires_action":        "Card requires 3D Secure authentication",
            "requires_payment_method":"Card was declined — please try a different card",
            "processing":             "Payment is being processed",
            "canceled":               "Payment was canceled",
        }.get(status, f"Payment status: {status}")

    # ── Refund a charge ──────────────────────────────────────────────────────
    async def refund_payment(
        self,
        *,
        payment_intent_id: str,
        amount: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """
        Refund a charge. Pass amount in dollars for partial refund; omit
        for full refund.
        """
        if not self.configured():
            raise StripeError("Stripe not configured")

        params: list[tuple[str, str]] = [
            ("payment_intent", payment_intent_id),
        ]
        if amount is not None:
            params.append(("amount", str(int(round(amount * 100)))))
        if reason and reason in ("duplicate", "fraudulent", "requested_by_customer"):
            params.append(("reason", reason))

        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type":  "application/x-www-form-urlencoded",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{STRIPE_API_URL}/refunds", data=params, headers=headers)
        return resp.json()

    # ── Webhook signature verification ───────────────────────────────────────
    def verify_webhook(self, raw_body: bytes, signature_header: str, tolerance: int = 300) -> bool:
        """
        Verify a Stripe webhook signature using STRIPE_WEBHOOK_SECRET.

        Stripe's signing format:
          t=<unix_ts>,v1=<hmac_sha256_hex>[,v0=...]

        Algorithm:
          payload = f"{t}.{raw_body}"
          expected = HMAC-SHA256(STRIPE_WEBHOOK_SECRET, payload).hex()
          compare against v1

        `tolerance` (seconds) protects against replay attacks. Stripe's
        recommendation is 5 minutes (300s).
        """
        if not self.webhook_secret:
            logger.warning("[stripe_direct] webhook verification skipped — no STRIPE_WEBHOOK_SECRET set")
            return False
        if not signature_header:
            return False

        # Parse "t=...,v1=...,v0=..." into a dict of lists (each scheme can repeat)
        parts: dict[str, list[str]] = {}
        for kv in signature_header.split(","):
            if "=" not in kv:
                continue
            k, v = kv.strip().split("=", 1)
            parts.setdefault(k, []).append(v)

        t_str = (parts.get("t") or [""])[0]
        v1_sigs = parts.get("v1") or []
        if not t_str or not v1_sigs:
            return False

        try:
            t = int(t_str)
        except ValueError:
            return False

        # Replay-attack protection
        if tolerance > 0 and abs(time.time() - t) > tolerance:
            logger.warning(f"[stripe_direct] webhook timestamp outside tolerance ({tolerance}s)")
            return False

        signed_payload = f"{t}.{raw_body.decode('utf-8', errors='replace')}"
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # Constant-time compare against each v1 signature
        for sig in v1_sigs:
            if hmac.compare_digest(expected, sig):
                return True
        return False
