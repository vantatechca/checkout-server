"""
services/whop.py

Direct Whop checkout integration (no Lasso, no bridge worker).

Flow:
  1. Backend creates a checkout configuration via POST /api/v1/checkout_configurations
     - Inline plan with plan_type=one_time and initial_price = order total
     - metadata.order_id = our portal order_id  → lets the webhook match back
  2. Whop responds with { id: "ch_xxx", plan: { id: "plan_xxx", ... }, purchase_url }
  3. Frontend mounts <div data-whop-checkout-plan-id=plan_xxx
                          data-whop-checkout-session=ch_xxx ...>
     so the customer pays inside an iframe (PCI stays on whop.com).
  4. Whop fires /webhooks/whop on completion → routes/webhooks.py matches by
     metadata.order_id and marks the order paid.

Cloaking: the plan title shown to the customer (and stored in Whop) is the
cloaked decoy title (WHOP_PLAN_TITLE). Real peptide names live only in our DB.
"""

from __future__ import annotations

import logging
import httpx

from config import settings

logger = logging.getLogger(__name__)

WHOP_API_BASE_PROD    = "https://api.whop.com/api/v1"
WHOP_API_BASE_SANDBOX = "https://sandbox-api.whop.com/api/v1"


class WhopError(Exception):
    pass


class WhopClient:
    def __init__(self):
        self.sandbox  = bool(getattr(settings, "WHOP_SANDBOX", False))
        self.api_base = WHOP_API_BASE_SANDBOX if self.sandbox else WHOP_API_BASE_PROD

        # Pick the credential set based on environment. Sandbox and production
        # are entirely separate Whop accounts with their own keys/companies.
        if self.sandbox:
            self.api_key    = settings.WHOP_SANDBOX_API_KEY
            self.company_id = settings.WHOP_SANDBOX_COMPANY_ID
            self.product_id = getattr(settings, "WHOP_SANDBOX_PRODUCT_ID", "") or ""
            env_label       = "SANDBOX"
            missing_key_var = "WHOP_SANDBOX_API_KEY"
            missing_co_var  = "WHOP_SANDBOX_COMPANY_ID"
        else:
            self.api_key    = settings.WHOP_API_KEY
            self.company_id = settings.WHOP_COMPANY_ID
            self.product_id = getattr(settings, "WHOP_PRODUCT_ID", "") or ""
            env_label       = "production"
            missing_key_var = "WHOP_API_KEY"
            missing_co_var  = "WHOP_COMPANY_ID"

        self.currency   = (settings.WHOP_CURRENCY or "cad").lower()
        self.plan_title = settings.WHOP_PLAN_TITLE or "Order"

        # Parse pre-created tier plans. When configured, each order is routed
        # to the closest tier rather than creating a brand-new inline plan.
        # Drastically reduces "unique plans per month" pattern that flags
        # Whop's automated review.
        raw_tiers = (
            settings.WHOP_SANDBOX_TIER_PLANS if self.sandbox
            else settings.WHOP_TIER_PLANS
        ) or ""
        self.tier_plans: list[tuple[float, str]] = self._parse_tier_plans(raw_tiers)
        self.tier_strategy = (getattr(settings, "WHOP_TIER_STRATEGY", "round_down") or "round_down").lower()

        if not self.api_key:
            raise WhopError(
                f"{missing_key_var} is not configured in .env "
                f"(WHOP_SANDBOX={self.sandbox})"
            )
        if not self.company_id:
            raise WhopError(
                f"{missing_co_var} is not configured in .env "
                f"(WHOP_SANDBOX={self.sandbox})"
            )

        logger.info(f"[Whop] Using {env_label} environment ({self.api_base})")
        if self.sandbox:
            logger.info("[Whop] No real charges — use test cards (4242 4242 4242 4242)")
        if self.tier_plans:
            tier_summary = ", ".join(f"${p:.0f}={pid[:12]}…" for p, pid in self.tier_plans)
            logger.info(f"[Whop] Tier plans active ({self.tier_strategy}): {tier_summary}")

    @staticmethod
    def _parse_tier_plans(raw: str) -> list[tuple[float, str]]:
        """
        Parse "49:plan_aaa,99:plan_bbb,199:plan_ccc" → [(49.0, "plan_aaa"), ...]
        sorted ascending by price. Silently skips malformed entries.
        """
        out: list[tuple[float, str]] = []
        for entry in (raw or "").split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            price_s, plan_id = entry.split(":", 1)
            try:
                price = float(price_s.strip())
            except ValueError:
                continue
            plan_id = plan_id.strip()
            if price <= 0 or not plan_id:
                continue
            out.append((price, plan_id))
        out.sort(key=lambda x: x[0])
        return out

    def _pick_tier(self, amount: float) -> tuple[str, float] | None:
        """
        Find the best tier for `amount` per WHOP_TIER_STRATEGY.
        Returns (plan_id, tier_price) or None if no tier fits and we should
        fall back to inline plan creation.
        """
        if not self.tier_plans:
            return None

        prices = [p for p, _ in self.tier_plans]
        min_price = prices[0]
        max_price = prices[-1]

        # Cart outside tier range — fall back to inline plan rather than
        # over/under-charge by a huge amount.
        if amount < min_price * 0.5 or amount > max_price * 1.5:
            logger.info(
                f"[Whop] Cart ${amount:.2f} outside tier range "
                f"(${min_price:.0f}-${max_price:.0f}) — using inline plan"
            )
            return None

        if self.tier_strategy == "round_up":
            for p, pid in self.tier_plans:
                if p >= amount:
                    return (pid, p)
            # Cart above largest tier → use largest
            return (self.tier_plans[-1][1], self.tier_plans[-1][0])

        if self.tier_strategy == "nearest":
            best = min(self.tier_plans, key=lambda t: abs(t[0] - amount))
            return (best[1], best[0])

        # Default: round_down — largest tier ≤ amount
        chosen = None
        for p, pid in self.tier_plans:
            if p <= amount:
                chosen = (pid, p)
            else:
                break
        if chosen is None:
            # Cart below smallest tier — use smallest (rare edge case)
            chosen = (self.tier_plans[0][1], self.tier_plans[0][0])
        return chosen

    def build_sink_email(self, order_id: str, customer_email: str | None) -> str:
        """
        If WHOP_SINK_EMAIL is configured, rewrite the customer's email to
        `{user}+{order_id}@{domain}` so Whop's auto-generated receipts go to
        your sink inbox instead of the customer. Returns the customer's real
        email unchanged when no sink is configured.

        Per-order alias means each Whop "customer" record looks unique
        (mimics normal shopper behavior) rather than all orders collapsing
        to one repeat customer.
        """
        sink = (getattr(settings, "WHOP_SINK_EMAIL", "") or "").strip()
        if not sink or "@" not in sink:
            return customer_email or ""

        user, _, domain = sink.partition("@")
        # Strip any existing +tag on the sink user so we always replace it
        user = user.split("+", 1)[0]
        # Sanitize order_id for email-local-part safety
        safe_oid = "".join(c for c in (order_id or "") if c.isalnum() or c in "-_.")
        if not safe_oid:
            return f"{user}@{domain}"
        return f"{user}+{safe_oid}@{domain}"

    # Allow-list of metadata keys we'll forward to Whop. Anything else passed
    # in extra_meta is silently dropped. This is the defensive layer that
    # stops accidental leakage of identifying fields (source_domain, store
    # name, etc.) into Whop's payment records — a Whop compliance reviewer
    # opening any transaction would otherwise see exactly which brand/site
    # the order originated from.
    _METADATA_ALLOWED_KEYS = {"order_id"}

    async def create_checkout_session(
        self,
        order_id:    str,
        amount:      float,
        email:       str | None = None,
        currency:    str | None = None,
        return_url:  str | None = None,
        extra_meta:  dict | None = None,
    ) -> dict:
        """
        Creates a one-time checkout configuration for `amount` and returns:
          {
            "session_id":   "ch_xxx",
            "plan_id":      "plan_xxx",
            "purchase_url": "https://whop.com/checkout/plan_xxx?session=ch_xxx",
            "amount":       <float>,
            "currency":     "cad",
          }

        Raises WhopError on any failure. `amount` is in major units (dollars,
        not cents) — Whop's `initial_price` field is a float in major units.
        """
        if amount <= 0:
            raise WhopError(f"Whop checkout amount must be > 0, got {amount}")

        cur = (currency or self.currency).lower()

        # Only forward whitelisted keys to Whop. extra_meta might come from
        # callers with internal fields (store_name, source_domain, etc.) that
        # we explicitly do NOT want in Whop's records.
        metadata = {"order_id": order_id}
        if extra_meta:
            for k, v in extra_meta.items():
                if k in self._METADATA_ALLOWED_KEYS and v is not None:
                    metadata[k] = str(v)

        # ── Tier routing ────────────────────────────────────────────────
        # If tier plans are configured, pick the closest one and reference
        # it by plan_id (no new plan created). Whop's dashboard sees a
        # stable set of N plans across all orders — looks like normal SaaS
        # pricing instead of "600 unique plans per month".
        tier_choice = self._pick_tier(float(amount))
        used_tier = tier_choice is not None
        charged_amount = float(amount)

        # Explicit payment method config — only card, no crypto.
        # `include_platform_defaults: False` means Whop ignores the
        # company-level dashboard defaults entirely; only what's in `enabled`
        # is shown. `disabled` is required by Whop's API to be present but
        # can be empty when we're using the include_platform_defaults=False
        # whitelist approach.
        # Valid payment method identifiers per Whop's 400 error message
        # include: card, apple_pay, alipay, afterpay_clearpay, klarna,
        # ca_bank_transfer, au_bank_transfer, etc.
        payment_method_configuration = {
            "enabled":  ["card"],
            "disabled": [],
            "include_platform_defaults": False,
        }

        body: dict
        if used_tier:
            tier_plan_id, tier_price = tier_choice
            charged_amount = tier_price
            logger.info(
                f"[Whop] Routing cart ${amount:.2f} → tier plan {tier_plan_id} "
                f"@ ${tier_price:.2f} (strategy={self.tier_strategy})"
            )
            # Variant 2 of checkout_configurations: reference existing plan_id
            body = {
                "mode": "payment",
                "plan_id": tier_plan_id,
                "metadata": metadata,
                "payment_method_configuration": payment_method_configuration,
            }
        else:
            # Fallback: create a one-time plan inline at the exact cart amount.
            # Used when no tiers configured or cart is outside tier range.
            plan_body: dict = {
                "company_id":        self.company_id,
                "currency":          cur,
                "initial_price":     round(float(amount), 2),
                "plan_type":         "one_time",
                "title":             self.plan_title,
                # Tell Whop the price already includes tax — Whop is a
                # merchant of record and would otherwise add VAT/GST on
                # top of our total.
                "override_tax_type": "inclusive",
            }
            if self.product_id:
                plan_body["product_id"] = self.product_id
            body = {
                "mode": "payment",
                "plan": plan_body,
                "metadata": metadata,
                "payment_method_configuration": payment_method_configuration,
            }

        if return_url:
            body["redirect_url"] = return_url

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        logger.info(
            f"[Whop] Creating checkout config for order={order_id} "
            f"amount={amount:.2f} {cur.upper()}"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"{self.api_base}/checkout_configurations",
                    json=body,
                    headers=headers,
                )
            except httpx.RequestError as e:
                raise WhopError(f"Whop API unreachable: {e}") from e

        if resp.status_code >= 400:
            raise WhopError(
                f"Whop API returned {resp.status_code}: {resp.text[:400]}"
            )

        try:
            data = resp.json()
        except Exception:
            raise WhopError(f"Whop API returned non-JSON: {resp.text[:300]}")

        session_id = data.get("id", "")
        plan       = data.get("plan", {}) or {}
        plan_id    = plan.get("id", "")
        purchase_url = data.get("purchase_url", "")

        if not session_id or not plan_id:
            raise WhopError(
                f"Whop response missing id/plan.id: {str(data)[:300]}"
            )

        logger.info(
            f"[Whop] Session created: session={session_id} plan={plan_id} "
            f"for order={order_id}"
        )

        # Compute the email we'll tell Whop is the customer's. If a sink is
        # configured this is a synthetic +alias on our domain; otherwise it's
        # the customer's real email. Frontend uses this (NOT the real email)
        # when calling wco.setEmail on the iframe, so Whop's records and any
        # receipt emails attach to the sink.
        whop_email = self.build_sink_email(order_id, email)

        return {
            "session_id":      session_id,
            "plan_id":         plan_id,
            "purchase_url":    purchase_url,
            "amount":          float(amount),       # original cart amount we got asked to charge
            "charged_amount":  charged_amount,      # actual amount Whop will charge (= tier price if tier used, else == amount)
            "tier_used":       used_tier,
            "currency":        cur,
            "sandbox":         self.sandbox,
            "whop_email":      whop_email,
        }
