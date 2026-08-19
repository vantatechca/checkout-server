"""
Shippo integration — shipping label rates + purchase for the admin
dashboard's "Shipping" tab.

Domestic shipments only (CA store → CA customer, US store → US customer —
this business has no cross-border pattern anywhere in the codebase). This
is a deliberate scope boundary, not an oversight: Shippo only requires a
package-contents description via the `customs_declaration` object, and that
object is exclusively for INTERNATIONAL shipments. A domestic rate lookup
or label purchase (address_from, address_to, parcel dimensions/weight, rate
selection) has no contents-description field at all. So this client never
builds a customs_declaration and never sends any product name (real or
decoy) anywhere near Shippo or the carrier — there's nothing to send in the
first place for a domestic label. If this business ever ships
internationally, that's a real customs-law question requiring its own
review; do not extend this client to fabricate a customs declaration.

Docs: https://docs.goshippo.com
"""
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

SHIPPO_BASE = "https://api.goshippo.com"


class ShippoError(Exception):
    pass


class ShippoClient:
    def __init__(self):
        self.api_token = getattr(settings, "SHIPPO_API_TOKEN", "") or ""

    def _headers(self) -> dict:
        return {
            "Authorization": f"ShippoToken {self.api_token}",
            "Content-Type":  "application/json",
        }

    def _from_address(self, order) -> dict:
        """Ship-from address — CA or US block, selected by order.currency
        (CAD→CA, USD→US), same rule already used for pymtz."""
        us = (order.currency or "").upper() == "USD"
        suffix = "US" if us else "CA"
        addr = {
            "name":    getattr(settings, f"SHIPPO_FROM_NAME_{suffix}", "") or "",
            "street1": getattr(settings, f"SHIPPO_FROM_ADDRESS1_{suffix}", "") or "",
            "street2": getattr(settings, f"SHIPPO_FROM_ADDRESS2_{suffix}", "") or "",
            "city":    getattr(settings, f"SHIPPO_FROM_CITY_{suffix}", "") or "",
            "phone":   getattr(settings, f"SHIPPO_FROM_PHONE_{suffix}", "") or "",
            "country": "US" if us else "CA",
        }
        if us:
            addr["state"] = getattr(settings, "SHIPPO_FROM_STATE_US", "") or ""
            addr["zip"]   = getattr(settings, "SHIPPO_FROM_ZIP_US", "") or ""
        else:
            addr["state"] = getattr(settings, "SHIPPO_FROM_PROVINCE_CA", "") or ""
            addr["zip"]   = getattr(settings, "SHIPPO_FROM_POSTAL_CA", "") or ""
        return addr

    def default_from_address(self, order) -> dict:
        """Public wrapper — used to prefill the admin's Buy Label form with
        the configured CA/US default so it isn't typed from scratch every
        time, while still letting the admin edit it before purchase."""
        return self._from_address(order)

    def _to_address(self, order) -> dict:
        """Ship-to address — straight from the order's shipping fields,
        already on the Order row."""
        return {
            "name":    f"{order.first_name or ''} {order.last_name or ''}".strip(),
            "street1": order.address1 or "",
            "street2": order.address2 or "",
            "city":    order.city or "",
            "state":   order.province or "",
            "zip":     order.postal_code or "",
            "country": order.country or "",
            "phone":   order.phone or "",
        }

    async def get_rates(
        self,
        order,
        *,
        weight_oz:    float,
        length_in:    float,
        width_in:     float,
        height_in:    float,
        from_address: Optional[dict] = None,
    ) -> list[dict]:
        """
        Creates a Shippo shipment (address_from + address_to + parcel) and
        returns the live carrier rates. No customs_declaration — see module
        docstring. Returns a normalized, price-sorted list.

        from_address: admin-edited override from the Buy Label form (prefilled
        with default_from_address() but editable). Falls back to the
        configured CA/US default when not supplied.
        """
        if not self.api_token:
            raise ShippoError("SHIPPO_API_TOKEN not configured")

        body = {
            "address_from": from_address or self._from_address(order),
            "address_to":   self._to_address(order),
            "parcel": {
                "length":      str(length_in),
                "width":       str(width_in),
                "height":      str(height_in),
                "distance_unit": "in",
                "weight":      str(weight_oz),
                "mass_unit":   "oz",
            },
            "async": False,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHIPPO_BASE}/shipments/",
                headers=self._headers(),
                json=body,
            )
        if resp.status_code not in (200, 201):
            raise ShippoError(f"Shippo rate lookup failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        rates = data.get("rates") or []
        if not rates:
            messages = data.get("messages") or []
            raise ShippoError(f"Shippo returned no rates: {messages or data}")

        normalized = [
            {
                "rate_id":        r["object_id"],
                "carrier":        r.get("provider", ""),
                "servicelevel":   (r.get("servicelevel") or {}).get("name", ""),
                "amount":         float(r.get("amount", 0)),
                "currency":       r.get("currency", ""),
                "estimated_days": r.get("estimated_days"),
            }
            for r in rates
        ]
        normalized.sort(key=lambda r: r["amount"])
        return normalized

    async def buy_label(self, *, rate_id: str, order_id: str, carrier: str) -> dict:
        """
        Purchases a label for a previously-quoted rate. `carrier` is passed
        through from the rate the caller already selected (from get_rates)
        rather than re-derived from Shippo's transaction response, which
        doesn't reliably re-include it.

        The only per-order text sent to Shippo is `order_id`, via Shippo's
        own `metadata` field (for our reconciliation in Shippo's dashboard,
        never seen by the carrier) — no product name, real or decoy.
        """
        if not self.api_token:
            raise ShippoError("SHIPPO_API_TOKEN not configured")

        body = {
            "rate":            rate_id,
            "label_file_type": "PDF",
            "async":           False,
            "metadata":        order_id,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHIPPO_BASE}/transactions/",
                headers=self._headers(),
                json=body,
            )
        if resp.status_code not in (200, 201):
            raise ShippoError(f"Shippo label purchase failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        if data.get("status") != "SUCCESS":
            messages = data.get("messages") or []
            raise ShippoError(f"Shippo label purchase not successful: {messages or data}")

        return {
            "tracking_number": data.get("tracking_number", ""),
            "tracking_url":    data.get("tracking_url_provider", ""),
            "carrier":         carrier,
            "label_url":       data.get("label_url", ""),
            "transaction_id":  data.get("object_id", ""),
        }
