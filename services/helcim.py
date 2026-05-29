"""
Helcim credit card processing.

Flow:
  1. Frontend uses HelcimPay.js (hosted fields) to tokenize card → gets helcimPayToken
  2. Frontend POSTs { helcimPayToken, amount, orderId, ... } to our backend
  3. We call Helcim /helcim-pay/initialize then /card-transactions to charge

Docs: https://devdocs.helcim.com/
"""
import httpx
from config import settings


class HelcimClient:
    def __init__(self, api_token: str | None = None):
        self.api_token = api_token or settings.HELCIM_API_TOKEN
        self.base_url  = settings.HELCIM_API_URL

    def _headers(self) -> dict:
        return {
            "api-token":    self.api_token,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    async def initialize_payment(
        self,
        amount_cents: int,
        currency: str = "CAD",
        order_id: str = "",
    ) -> dict:
        """
        Initialize a HelcimPay.js session.
        Returns { checkoutToken } used by the frontend JS widget.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/helcim-pay/initialize",
                headers=self._headers(),
                json={
                    "paymentType":   "purchase",
                    "amount":        amount_cents / 100,
                    "currency":      currency,
                    "customerCode":  order_id,
                    "invoiceNumber": order_id,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def charge_card(
        self,
        *,
        helcim_pay_token: str,
        amount: float,
        currency: str = "CAD",
        order_id: str,
        ip_address: str = "",
        customer_name: str = "",
        customer_email: str = "",
    ) -> dict:
        """
        Process a card charge using a HelcimPay token.
        Returns the full Helcim transaction response.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/card-transactions",
                headers=self._headers(),
                json={
                    "ipAddress":      ip_address,
                    "ecommerce":      True,
                    "helcimPayToken": helcim_pay_token,
                    "invoiceNumber":  order_id,
                    "currency":       currency,
                    "amount":         amount,
                    "customerName":   customer_name,
                    "customerEmail":  customer_email,
                },
            )
            data = resp.json()

            if resp.status_code != 200 or not data.get("data", {}).get("approved"):
                raise HelcimError(
                    data.get("errors", [{"message": "Card declined"}])[0]["message"]
                )

            return data

    async def refund(self, transaction_id: str, amount: float) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/card-transactions/{transaction_id}/refund",
                headers=self._headers(),
                json={"amount": amount},
            )
            resp.raise_for_status()
            return resp.json()


class HelcimError(Exception):
    pass
