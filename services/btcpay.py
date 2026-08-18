"""
BTCPay Server integration.

BTCPay handles:
  - All crypto coin selection (Bitcoin native + Boltz swaps for alts/stables)
  - QR code generation
  - Payment confirmation & expiry timers
  - Webhook notifications back to us

Our job:
  1. Create invoice → get hosted checkout URL → redirect customer
  2. Receive webhook → update order status

Docs: https://docs.btcpayserver.org/API/Greenfield/v1/
"""
import hmac
import hashlib
import httpx
from datetime import datetime
from config import settings


class BTCPayClient:
    def __init__(
        self,
        store_id: str | None = None,
        api_key: str | None = None,
    ):
        self.store_id = store_id or settings.BTCPAY_STORE_ID
        self.api_key  = api_key  or settings.BTCPAY_API_KEY
        self.base_url = settings.BTCPAY_URL.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self.api_key}",
            "Content-Type":  "application/json",
        }

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount: float,
        currency: str = "CAD",
        customer_email: str = "",
        customer_name: str = "",
        redirect_url: str = "",
        webhook_url: str = "",
    ) -> dict:
        """
        Creates a BTCPay invoice and returns the full response dict.
        Key fields: id, checkoutLink, expirationTime, status
        """
        payload = {
            "amount":   str(amount),
            "currency": currency,
            "metadata": {
                "orderId":    order_id,
                "buyerEmail": customer_email,
                "buyerName":  customer_name,
            },
            "checkout": {
                "redirectURL":         redirect_url or f"{settings.BASE_URL}/order/{order_id}/confirmation",
                "redirectAutomatically": True,
                "expirationMinutes":   60,
            },
        }

        if webhook_url:
            payload["notificationUrl"] = webhook_url

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/stores/{self.store_id}/invoices",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code not in (200, 201):
                raise BTCPayError(f"BTCPay invoice creation failed: {resp.text}")
            return resp.json()

    async def get_invoice(self, invoice_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/stores/{self.store_id}/invoices/{invoice_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_invoice_payment_methods(self, invoice_id: str) -> list:
        """Returns which coins the customer paid with + amounts."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/stores/{self.store_id}/invoices/{invoice_id}/payment-methods",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()


def verify_btcpay_webhook(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Validate BTCPay webhook HMAC-SHA256 signature.
    Header format: "sha256=<hex_digest>"
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.BTCPAY_WEBHOOK_SECRET.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)


# BTCPay invoice status mapping to our PaymentStatus
BTCPAY_STATUS_MAP = {
    "New":        "pending",
    "Processing": "pending",
    "Expired":    "expired",
    "Invalid":    "failed",
    "Settled":    "paid",
    "Complete":   "paid",    # older BTCPay versions
}


class BTCPayError(Exception):
    pass
