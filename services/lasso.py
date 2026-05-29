"""
services/lasso.py

Lasso checkout integration.

Flow:
  1. POST /api/cart-sessions/sync-cart  →  get session_id
  2. Redirect customer to LASSO_CHECKOUT_URL?sid=SESSION_ID
  3. Lasso/Whop processes payment
  4. Whop fires a webhook → /webhooks/whop  (marks order paid)

All cart items are cloaked (universal decoy title) before being sent.
Real product names never leave our server.
"""

from __future__ import annotations

import logging
import httpx

from config import settings

logger = logging.getLogger(__name__)

# Lasso API base — their production endpoint
LASSO_API_BASE = "https://api.lassocheckout.com/api"


class LassoError(Exception):
    pass


class LassoClient:
    def __init__(self):
        self.store_id     = settings.LASSO_STORE_ID
        self.checkout_url = settings.LASSO_CHECKOUT_URL.rstrip("/")

        if not self.store_id:
            raise LassoError("LASSO_STORE_ID is not configured in .env")
        if not self.checkout_url:
            raise LassoError("LASSO_CHECKOUT_URL is not configured in .env")

    async def create_session(
        self,
        cart: list[dict],       # already-cloaked items from build_lasso_cart()
        currency: str = "CAD",
        country:  str = "CA",
        order_id: str | None = None,
    ) -> str:
        """
        Syncs the cloaked cart with Lasso and returns the session_id.
        Raises LassoError on failure.
        """
        payload: dict = {
            "storeId":     self.store_id,
            "currentCart": cart,
            "currency":    currency,
            "country":     country,
        }

        # Pass our internal order_id as metadata so the Whop webhook can
        # match back to this order without ambiguity.
        if order_id:
            payload["metadata"] = {"order_id": order_id}

        logger.info(f"[Lasso] Creating session for order={order_id} items={len(cart)}")

        async with httpx.AsyncClient(timeout=12.0) as client:
            try:
                resp = await client.post(
                    f"{LASSO_API_BASE}/cart-sessions/sync-cart",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:300]
                raise LassoError(
                    f"Lasso API returned {e.response.status_code}: {body}"
                ) from e
            except httpx.RequestError as e:
                raise LassoError(f"Lasso API unreachable: {e}") from e

        data = resp.json()
        session_id = data.get("session_id")

        if not session_id:
            raise LassoError(f"Lasso did not return session_id. Response: {data}")

        logger.info(f"[Lasso] Session created: {session_id} for order={order_id}")
        return session_id

    def build_redirect_url(self, session_id: str) -> str:
        """Returns the full Lasso checkout URL with sid param."""
        return f"{self.checkout_url}?sid={session_id}"