"""
WPay 2D (Direct Card) via WordPress + the WPay Channels WooCommerce plugin.

Same architecture as services/onramp_wp.py, reusing the SAME WordPress site
(and the same REST API credentials) — the WPay Channels plugin is installed
there alongside the 2530gateway one, each registered as its own WooCommerce
gateway ID.

Flow:
  1. Customer picks "Credit Card (WPay 2D)" on our FastAPI checkout.
  2. We create a WooCommerce order via WC REST API on that WP site, with
     payment_method set to the WPay 2D gateway ID. Our internal order_id is
     stored in meta_data.
  3. WC returns a `payment_url` (the pay-for-order URL). We redirect the
     customer there — the plugin's own card form (tokenized via Basis
     Theory) takes over from that point, so raw card data never touches
     this backend, same as it never would for onramp_wp.
  4. WC marks the order paid and fires a webhook to /webhooks/wpay_2d.
     We match by our order_id (stored in WC meta_data) and mark our local
     order paid.

Why reuse onramp_wp's approach instead of calling WPay's API directly here:
  * The plugin's WPay 2D gateway only runs inside WooCommerce — same
    constraint as 2530gateway. We route through WC's REST API instead of
    reimplementing the plugin's Basis Theory tokenization + proxy-
    substitution logic in Python (see services/wpay.py's module docstring
    for why that matters: raw card data must never reach this backend).

Auth: WooCommerce REST API uses HTTP Basic with `consumer_key:consumer_secret`,
or an Application Password — same credentials as ONRAMP_WP_*, since it's the
same site.
"""
import base64
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Same WC REST API base convention as onramp_wp.py — query-param form works
# regardless of the site's permalink settings.
WC_API_BASE = "/?rest_route=/wc/v3"

# Per MERCHANT_SETUP.txt: "Minimum order: $5 USD in plugin (your WPay account
# may require $10+)" — lower than HPP's documented $20 (services/wpay.py's
# MIN_AMOUNT), since this is the 2D/direct gateway, not HPP.
MIN_AMOUNT = 5.0


class WPayWPError(Exception):
    pass


class WPayWPClient:
    """Talks to the same WordPress site as OnrampWPClient, via WC REST API,
    routing to the WPay Channels plugin's "WPay 2D" gateway instead."""

    def __init__(self):
        # Same site, same credentials as onramp_wp — intentionally reused,
        # not duplicated in .env, since it's the same WordPress install.
        self.base_url = (getattr(settings, "ONRAMP_WP_URL", "") or "").rstrip("/")
        self.consumer_key    = getattr(settings, "ONRAMP_WP_CONSUMER_KEY", "") or ""
        self.consumer_secret = getattr(settings, "ONRAMP_WP_CONSUMER_SECRET", "") or ""
        self.username        = getattr(settings, "ONRAMP_WP_USERNAME", "") or ""
        self.app_password    = (getattr(settings, "ONRAMP_WP_APP_PASSWORD", "") or "").replace(" ", "")

        raw_pid = str(getattr(settings, "WPAY_WP_PRODUCT_ID", "") or "").strip()
        self.product_id = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else 0
        self.gateway_id = getattr(settings, "WPAY_WP_GATEWAY_ID", "") or "wpay_2d"

    def _request_kwargs(self) -> dict:
        if self.username and self.app_password:
            token = base64.b64encode(
                f"{self.username}:{self.app_password}".encode()
            ).decode()
            return {"headers": {"Authorization": f"Basic {token}"}}
        return {"params": {
            "consumer_key":    self.consumer_key,
            "consumer_secret": self.consumer_secret,
        }}

    def configured(self) -> bool:
        has_app_auth = bool(self.username and self.app_password)
        has_wc_auth  = bool(self.consumer_key and self.consumer_secret)
        return bool(self.base_url and (has_app_auth or has_wc_auth))

    async def create_order(
        self,
        *,
        external_order_id: str,
        amount:      float,
        currency:    str,
        first_name:  str,
        last_name:   str,
        email:       str,
        phone:       str = "",
        address1:    str = "",
        address2:    str = "",
        city:        str = "",
        state:       str = "",
        postal_code: str = "",
        country:     str = "",
    ) -> dict:
        """
        Create a WooCommerce order routed to the WPay 2D gateway. Returns the
        WC order dict; `payment_url` is where the customer should be redirected.
        """
        if not self.configured():
            raise WPayWPError(
                "WPay (WP plugin) not configured — set ONRAMP_WP_URL and "
                "either ONRAMP_WP_CONSUMER_KEY/SECRET or ONRAMP_WP_USERNAME/"
                "APP_PASSWORD in .env (shared with the onramp_wp integration)"
            )

        amt = f"{float(amount):.2f}"
        body = {
            "payment_method":       self.gateway_id,
            "payment_method_title": "Credit / Debit Card",
            # `set_paid: false` keeps the order pending; the customer
            # completes payment via the gateway's card form on the
            # pay-for-order page.
            "set_paid":             False,
            "currency":             (currency or "USD").upper(),
            "billing": {
                "first_name": first_name,
                "last_name":  last_name,
                "email":      email,
                "phone":      phone,
                "address_1":  address1,
                "address_2":  address2,
                "city":       city,
                "state":      (state or "").upper(),
                "postcode":   postal_code,
                "country":    (country or "").upper(),
            },
            "meta_data": [
                {"key": "_external_order_id", "value": external_order_id},
                {"key": "_external_source",   "value": "fastapi-checkout"},
            ],
        }

        if self.product_id:
            body["line_items"] = [{
                "product_id": self.product_id,
                "quantity":   1,
                "total":      amt,
                "subtotal":   amt,
            }]
        else:
            body["fee_lines"] = [{
                "name":       "Order",
                "total":      amt,
                "tax_status": "none",
            }]

        url = f"{self.base_url}{WC_API_BASE}/orders"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, **self._request_kwargs())

        if resp.status_code not in (200, 201):
            raise WPayWPError(
                f"WC order create failed ({resp.status_code}): {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except Exception:
            raise WPayWPError(
                f"WC order create returned non-JSON body (status {resp.status_code}, "
                f"headers={dict(resp.headers)}): {resp.content[:500]!r}"
            )
        if not data.get("payment_url"):
            raise WPayWPError(
                f"WC response missing payment_url for order {data.get('id')}: {data}"
            )
        return data

    async def get_order(self, wc_order_id: int) -> dict:
        """Fetch a WC order's current state — used for polling fallback."""
        url = f"{self.base_url}{WC_API_BASE}/orders/{wc_order_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, **self._request_kwargs())
            resp.raise_for_status()
            return resp.json()


# WC order status → our PaymentStatus value. Same mapping as onramp_wp.py —
# it's the same WooCommerce install's status vocabulary either way.
WC_STATUS_MAP = {
    "pending":    "pending",
    "on-hold":    "pending",
    "processing": "paid",
    "completed":  "paid",
    "failed":     "failed",
    "cancelled":  "cancelled",
    "refunded":   "refunded",
}
