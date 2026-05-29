"""
Shopify order creation service.
Called when an order is marked as paid in the admin dashboard.
"""
import logging
import httpx
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2024-07"


async def create_shopify_order(order) -> Optional[dict]:
    """
    Creates a paid order in Shopify when manually marked as paid.
    Returns the Shopify order dict or None if failed.
    """
    # Pick Shopify admin store based on order currency
    # USD orders (always Zelle, or Crypto from US stores) → US admin store
    # CAD orders (Interac, or Crypto from CA stores) → CA admin store
    is_us_order = (order.currency or "CAD").upper() == "USD"

    if is_us_order:
        store_domain = settings.SHOPIFY_STORE_DOMAIN_US
        api_token    = settings.SHOPIFY_API_TOKEN_US
        store_label  = "US"
    else:
        store_domain = settings.SHOPIFY_STORE_DOMAIN
        api_token    = settings.SHOPIFY_API_TOKEN
        store_label  = "CA"

    if not store_domain or not api_token:
        logger.warning(
            f"Shopify {store_label} credentials not configured — "
            f"skipping order creation for {order.id} (currency={order.currency})"
        )
        return None

    base_url = f"https://{store_domain}/admin/api/{SHOPIFY_API_VERSION}"
    headers = {
        "X-Shopify-Access-Token": api_token,
        "Content-Type": "application/json",
    }

    # Determine payment method label + zero-price flag
    method_labels = {
        "interac": "Interac e-Transfer",
        "zelle":   "Zelle",
        "card":    "Credit Card",
        "crypto":  "Cryptocurrency",
        "altcoin": "Altcoin (NowPayments)",
    }
    payment_label = method_labels.get(order.payment_method, order.payment_method)

    # Zelle / Interac / Crypto / Altcoin are collected OUTSIDE Shopify — zero-price them
    zero_price = order.payment_method in ("zelle", "interac", "crypto", "altcoin")

    # Build line items — variant appended to title
    line_items = []
    for item in order.items:
        title = item.title
        if item.variant:
            title = f"{item.title} — {item.variant}"

        line_item = {
            "title":             title,
            "quantity":          item.qty,
            "price":             "0.00" if zero_price else str(item.price),
            "requires_shipping": True,
            "taxable":           False,
        }
        line_items.append(line_item)

    # Build customer
    customer = {
        "first_name": order.first_name or "Customer",
        "last_name": order.last_name or "",
        "email": order.email,
    }

    # Build shipping address
    shipping_address = {
        "first_name": order.first_name or "Customer",
        "last_name": order.last_name or "",
        "address1": order.address1 or "",
        "address2": order.address2 or "",
        "city": order.city or "",
        "province": order.province or "",
        "zip": order.postal_code or "",
        "country": order.country or "CA",
        "phone": order.phone or "",
    }

    # ─── Build note + tags ──────────────────────────────────────────────────
    # Notes: "MPC | AFFILIATE_CODE:TJWIN" or just one of them or none
    note_parts = []
    tag_parts  = []

    source = (order.source_domain or "").lower()
    if "montrealpeptides.ca" in source or "i81gwq-sk.myshopify.com" in source:
        note_parts.append("MPC")
        tag_parts.append("MPC")

    if order.discount_code:
        code = order.discount_code.upper()
        note_parts.append(f"AFFILIATE_CODE:{code}")
        tag_parts.append(f"DISCOUNT:{code}")

    note_str = " | ".join(note_parts) if note_parts else None
    tags_str = ", ".join(tag_parts) if tag_parts else None

    # Build the order payload
    payload = {
        "order": {
            "line_items": line_items,
            "customer": customer,
            "shipping_address": shipping_address,
            "billing_address": shipping_address,
            "email": order.email,
            "financial_status": "paid",
            "currency": order.currency or "CAD",
            "send_receipt": False,
            "send_fulfillment_receipt": True,
        }
    }

    if note_str:
        payload["order"]["note"] = note_str
    if tags_str:
        payload["order"]["tags"] = tags_str

    # Only add a transactions block for non-zero orders (Shopify rejects $0 sales)
    if not zero_price:
        payload["order"]["transactions"] = [
            {
                "kind":     "sale",
                "status":   "success",
                "amount":   str(order.total),
                "currency": order.currency or "CAD",
                "gateway":  payment_label,
            }
        ]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/orders.json",
                json=payload,
                headers=headers,
            )

        if response.status_code == 201:
            shopify_order = response.json().get("order", {})
            logger.info(
                f"✅ Shopify {store_label} order created: #{shopify_order.get('order_number')} "
                f"for {order.id} (method={order.payment_method}, zero_priced={zero_price}, "
                f"note={note_str or 'none'}, tags={tags_str or 'none'})"
            )
            return shopify_order
        else:
            logger.error(f"❌ Shopify order creation failed: {response.status_code} — {response.text}")
            return None

    except Exception as e:
        logger.error(f"❌ Shopify API error: {e}")
        return None