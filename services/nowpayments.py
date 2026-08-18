"""
NowPayments integration — Altcoin payments via hosted invoice page.
Handles ETH, USDT, LTC, XMR, SOL, DOGE, BNB, TRX, MATIC, and 300+ more coins.

Docs: https://documenter.getpostman.com/view/7907941/S1a32n38
"""
import hashlib
import hmac
import json
import httpx
from config import settings

NOWPAYMENTS_BASE = "https://api.nowpayments.io/v1"


class NowPaymentsClient:
    def __init__(self):
        self.api_key = settings.NOWPAYMENTS_API_KEY

    def _headers(self) -> dict:
        return {
            "x-api-key":    self.api_key,
            "Content-Type": "application/json",
        }

    async def create_invoice(
        self,
        *,
        order_id:         str,
        amount:           float,
        currency:         str = "CAD",
        ipn_callback_url: str = "",
        success_url:      str = "",
        cancel_url:       str = "",
    ) -> dict:
        """
        Creates a hosted NowPayments invoice page.
        Customer lands on this page and selects their preferred altcoin.
        Returns: { id, invoice_url, ... }
        """
        payload = {
            "price_amount":      amount,
            "price_currency":    currency.lower(),
            "order_id":          order_id,
            "order_description": f"Order {order_id}",
            "ipn_callback_url":  ipn_callback_url,
            "success_url":       success_url,
            "cancel_url":        cancel_url,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{NOWPAYMENTS_BASE}/invoice",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code not in (200, 201):
                raise NowPaymentsError(f"NowPayments invoice creation failed: {resp.text}")
            return resp.json()

    async def get_payment(self, payment_id: str) -> dict:
        """Fetch current status of a payment by NowPayments payment ID."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{NOWPAYMENTS_BASE}/payment/{payment_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()


def verify_nowpayments_ipn(payload_bytes: bytes, sig_header: str) -> bool:
    if not sig_header:
        return False
    try:
        data = json.loads(payload_bytes)
        # NowPayments signs with RECURSIVE key sort (matches their PHP tksort
        # and JS sortObject reference). sort_keys=True sorts at every level,
        # including the nested `fee` object that appears in finished/partially_paid.
        sorted_str = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = hmac.new(
            settings.NOWPAYMENTS_IPN_SECRET.encode(),
            sorted_str.encode(),
            hashlib.sha512,
        ).hexdigest()
        match = hmac.compare_digest(expected, sig_header)
        if not match:
            import logging
            logging.getLogger(__name__).warning(
                f"IPN sig mismatch | keys: {list(data.keys())} | "
                f"expected: {expected[:20]}... | got: {sig_header[:20]}..."
            )
        return match
    except Exception:
        return False


# NowPayments payment_status → our PaymentStatus
NOWPAYMENTS_STATUS_MAP = {
    "waiting":        "pending",
    "confirming":     "pending",
    "confirmed":      "pending",
    "sending":        "pending",
    "partially_paid": "pending",   # flagged separately as underpaid
    "finished":       "paid",
    "failed":         "failed",
    "refunded":       "refunded",
    "expired":        "expired",
}


class NowPaymentsError(Exception):
    pass