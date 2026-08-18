"""
Onramp via WordPress + 2530gateway plugin.

Flow:
  1. Customer picks "Card (Alt)" on our FastAPI checkout.
  2. We create a WooCommerce order via WC REST API on the WP site that hosts
     the Insta-Onramp plugin. The line item is a single "Custom Order" product
     with our amount overridden. Our internal order_id is stored in meta_data.
  3. WC returns a `payment_url` (the pay-for-order URL). We redirect the
     customer there.
  4. Customer hits WC's pay page → the plugin redirects them to
     checkout.2530gateway.com → they pay card → onramp converts to USDC →
     USDC lands at the wallet configured in the plugin settings.
  5. WC marks the order paid and fires a webhook to /webhooks/onramp_wp.
     We match by our order_id (stored in WC meta_data) and mark our local
     order paid.

Why this design:
  * 2530gateway has no public REST API outside the WP plugin. The plugin
    only runs inside WooCommerce, so we route through WC's REST API instead.
  * Plugin handles the actual onramp routing and KYC — we just hand off
    the amount and customer details, then watch for the paid webhook.

Auth: WooCommerce REST API uses HTTP Basic with `consumer_key:consumer_secret`.
"""
import base64
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

# WooCommerce REST API base. We use the ?rest_route= format instead of the
# pretty /wp-json/... format because some WP installs (notably the one at
# peps-checkout.com) have permalinks set to "Plain", which disables the
# /wp-json/ rewrites. The query-param form works regardless of permalink
# settings — it's WP's universal fallback.
WC_API_BASE = "/?rest_route=/wc/v3"

# Plugin's gateway ID for the hosted Insta-Onramp option. Must match the ID
# WooCommerce uses for the enabled gateway in the plugin.
DEFAULT_GATEWAY_ID = "instaonrampdotto-instant-payment-gateway-hostedinstaonrampdotto"


class OnrampWPError(Exception):
    pass


class OnrampWPClient:
    """Talks to a WordPress site running the 2530gateway plugin via WC REST API."""

    def __init__(self):
        self.base_url = (getattr(settings, "ONRAMP_WP_URL", "") or "").rstrip("/")
        self.consumer_key    = getattr(settings, "ONRAMP_WP_CONSUMER_KEY", "") or ""
        self.consumer_secret = getattr(settings, "ONRAMP_WP_CONSUMER_SECRET", "") or ""
        # Application Password auth — preferred over WC REST keys when the
        # site is HTTP. Strip any spaces from the app password (WP shows it
        # space-separated but those are display-only).
        self.username        = getattr(settings, "ONRAMP_WP_USERNAME", "") or ""
        self.app_password    = (getattr(settings, "ONRAMP_WP_APP_PASSWORD", "") or "").replace(" ", "")
        # Product ID is optional. When blank, orders are built with a single
        # WC fee_line carrying the amount — no product needed in WP.
        raw_pid = str(getattr(settings, "ONRAMP_WP_PRODUCT_ID", "") or "").strip()
        self.product_id      = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else 0
        self.gateway_id      = getattr(settings, "ONRAMP_WP_GATEWAY_ID", "") or DEFAULT_GATEWAY_ID

    def _request_kwargs(self) -> dict:
        """
        Build httpx kwargs for auth.
        - Prefer Application Password (works over HTTP via Basic Auth).
        - Fall back to WC REST consumer_key/secret as query params (HTTPS only).
        """
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
        # product_id is optional — fee_line fallback works without it.
        # Need EITHER (username + app_password) OR (consumer_key + consumer_secret).
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
        Create a WooCommerce order for this payment. Returns the WC order dict;
        the `payment_url` field is where the customer should be redirected.
        """
        if not self.configured():
            raise OnrampWPError(
                "Onramp WP not configured — set ONRAMP_WP_URL, "
                "ONRAMP_WP_CONSUMER_KEY, ONRAMP_WP_CONSUMER_SECRET in .env"
            )

        amt = f"{float(amount):.2f}"
        body = {
            "payment_method":       self.gateway_id,
            "payment_method_title": "Pay By Credit / Debit Card",
            # `set_paid: false` keeps the order in `pending` status; the customer
            # completes payment via the gateway redirect from the pay-for-order page.
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
            # Stash our order id so the webhook can match the WC order back to
            # ours when payment completes. Prefixed with `_` so it's hidden
            # from the WP admin order UI.
            "meta_data": [
                {"key": "_external_order_id", "value": external_order_id},
                {"key": "_external_source",   "value": "fastapi-checkout"},
            ],
        }

        if self.product_id:
            # Product mode — link to a configured "Custom Order" product, override
            # its price via the line_item total. Useful if WP admin wants to see
            # the order against a product in WC reports.
            body["line_items"] = [{
                "product_id": self.product_id,
                "quantity":   1,
                "total":      amt,
                "subtotal":   amt,
            }]
        else:
            # No product needed — WC supports fee-only orders. The fee_line
            # carries the amount with a cloaked name; no WP product setup required.
            body["fee_lines"] = [{
                "name":       "Order",
                "total":      amt,
                "tax_status": "none",
            }]

        url = f"{self.base_url}{WC_API_BASE}/orders"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, **self._request_kwargs())

        if resp.status_code not in (200, 201):
            raise OnrampWPError(
                f"WC order create failed ({resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()
        if not data.get("payment_url"):
            raise OnrampWPError(
                f"WC response missing payment_url for order {data.get('id')}: "
                f"{data}"
            )
        return data

    async def get_order(self, wc_order_id: int) -> dict:
        """Fetch a WC order's current state — used for polling fallback."""
        url = f"{self.base_url}{WC_API_BASE}/orders/{wc_order_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, **self._request_kwargs())
            resp.raise_for_status()
            return resp.json()


# WC order status → our PaymentStatus value. "processing" means paid in WC
# parlance (the order is now being processed by the merchant). "completed"
# means fully fulfilled.
WC_STATUS_MAP = {
    "pending":    "pending",     # awaiting payment
    "on-hold":    "pending",     # awaiting payment confirmation
    "processing": "paid",        # paid, being processed
    "completed":  "paid",        # paid + fulfilled
    "failed":     "failed",
    "cancelled":  "cancelled",
    "refunded":   "refunded",
}
