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


class ShopifyOrderError(Exception):
    """
    Raised when Shopify order creation fails for a real reason (bad/expired
    API key, Shopify rejecting the request, network error) — as opposed to
    the store simply not having Shopify configured, which is an accepted,
    silent skip (see the `if not store_domain or not api_token` branch
    below). Callers use this to surface an actionable error instead of the
    failure only ever showing up in server logs.
    """
    pass


async def create_shopify_order(order) -> Optional[dict]:
    """
    Creates a paid order in Shopify when marked as paid.
    Returns the Shopify order dict, or None if this store has no Shopify
    credentials configured (an intentional, silent skip — not every store
    uses Shopify). Raises ShopifyOrderError if credentials ARE configured
    but the API call itself fails.
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

    # Princeton-only research-info fields — surfaced in the Shopify order
    # note so fulfillment sees the customer's company + declared field of use
    # without needing to open the internal admin dashboard.
    if getattr(order, "company", None):
        note_parts.append(f"COMPANY:{order.company}")
    if getattr(order, "research_field", None):
        note_parts.append(f"RESEARCH:{order.research_field}")

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
            # 401/403 here almost always means an expired/revoked API token —
            # the single most common real-world cause of this failing.
            logger.error(f"❌ Shopify order creation failed: {response.status_code} — {response.text}")
            raise ShopifyOrderError(
                f"Shopify {store_label} store rejected the order "
                f"({response.status_code}): {response.text[:300]}"
            )

    except ShopifyOrderError:
        raise
    except Exception as e:
        logger.error(f"❌ Shopify API error: {e}")
        raise ShopifyOrderError(f"Could not reach Shopify {store_label} store: {e}")


def _shopify_store_creds(store: str) -> tuple[str, str]:
    is_us = store.upper() == "US"
    store_domain = settings.SHOPIFY_STORE_DOMAIN_US if is_us else settings.SHOPIFY_STORE_DOMAIN
    api_token    = settings.SHOPIFY_API_TOKEN_US if is_us else settings.SHOPIFY_API_TOKEN
    return store_domain, api_token


def store_for_currency(currency: str) -> str:
    """CAD -> CA store, everything else -> US store — same rule
    create_shopify_order() itself uses to pick which store an order's
    Shopify order was created in. Used to look up the right store's
    credentials when later fulfilling that same Shopify order."""
    return "US" if (currency or "CAD").upper() == "USD" else "CA"


async def list_unfulfilled_orders(store: str = "CA") -> list[dict]:
    """
    Fetches paid-but-unfulfilled orders from the given Shopify store (CA or
    US), for the admin dashboard's bulk shipping-label workflow. These
    orders live only in Shopify, not our own `orders` table — the returned
    dicts are a normalized read-only view, not an Order.to_dict() shape.

    Returns [] if this store has no Shopify credentials configured (same
    silent-skip convention as create_shopify_order — not every deployment
    has both CA and US stores wired up).
    """
    store_domain, api_token = _shopify_store_creds(store)
    if not store_domain or not api_token:
        return []

    base_url = f"https://{store_domain}/admin/api/{SHOPIFY_API_VERSION}"
    headers = {"X-Shopify-Access-Token": api_token, "Content-Type": "application/json"}
    params = {
        "fulfillment_status": "unfulfilled",
        "financial_status":   "paid",
        "status":             "open",
        "limit":              250,
    }

    orders: list[dict] = []
    url = f"{base_url}/orders.json"
    first = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, headers=headers, params=(params if first else None))
            first = False
            if resp.status_code != 200:
                raise ShopifyOrderError(
                    f"Shopify {store} store rejected the unfulfilled-orders "
                    f"request ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            for o in data.get("orders", []):
                addr = o.get("shipping_address") or {}
                total_weight_g = sum(
                    (li.get("grams") or 0) * (li.get("quantity") or 1)
                    for li in (o.get("line_items") or [])
                )
                orders.append({
                    "ref":         f"shopify:{store.upper()}:{o['id']}",
                    "shopifyId":   o["id"],
                    "store":       store.upper(),
                    "orderNumber": o.get("name") or f"#{o.get('order_number')}",
                    "email":       o.get("email") or "",
                    "firstName":   addr.get("first_name") or "",
                    "lastName":    addr.get("last_name") or "",
                    "address1":    addr.get("address1") or "",
                    "address2":    addr.get("address2") or "",
                    "city":        addr.get("city") or "",
                    "province":    addr.get("province_code") or addr.get("province") or "",
                    "postalCode":  addr.get("zip") or "",
                    "country":     addr.get("country_code") or "",
                    "phone":       addr.get("phone") or o.get("phone") or "",
                    "currency":    o.get("currency") or "",
                    "totalPrice":  o.get("total_price") or "0.00",
                    "totalWeightG": total_weight_g,
                    "createdAt":   o.get("created_at"),
                })

            # Shopify paginates via the Link header, not an offset param —
            # the "next" URL already carries its own full querystring.
            url = None
            link = resp.headers.get("Link", "")
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")

    return orders


async def create_fulfillment(
    shopify_order_id: int,
    *,
    store: str,
    tracking_number: str,
    tracking_url: str,
    carrier: str,
) -> None:
    """
    Marks a Shopify order fulfilled after we've bought its shipping label —
    closes the loop so it drops out of the unfulfilled list on the next
    refresh. Uses the modern Fulfillment Orders flow (the old
    POST /orders/{id}/fulfillments.json endpoint this codebase's API version,
    2024-07, no longer supports).

    Best-effort: raises ShopifyOrderError on failure, but the label itself
    is already bought and paid for either way — callers should surface this
    as a warning, not roll anything back.
    """
    store_domain, api_token = _shopify_store_creds(store)
    if not store_domain or not api_token:
        raise ShopifyOrderError(f"Shopify {store} credentials not configured")

    base_url = f"https://{store_domain}/admin/api/{SHOPIFY_API_VERSION}"
    headers = {"X-Shopify-Access-Token": api_token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        fo_resp = await client.get(
            f"{base_url}/orders/{shopify_order_id}/fulfillment_orders.json",
            headers=headers,
        )
        if fo_resp.status_code != 200:
            raise ShopifyOrderError(
                f"Could not look up fulfillment orders for Shopify order "
                f"{shopify_order_id} ({fo_resp.status_code}): {fo_resp.text[:300]}"
            )
        fulfillment_orders = fo_resp.json().get("fulfillment_orders", [])
        open_fo = [fo for fo in fulfillment_orders if fo.get("status") in ("open", "in_progress")]
        if not open_fo:
            raise ShopifyOrderError(
                f"No open fulfillment orders for Shopify order {shopify_order_id} "
                "(already fulfilled?)"
            )

        payload = {
            "fulfillment": {
                "notify_customer": True,
                "tracking_info": {
                    "number":  tracking_number,
                    "url":     tracking_url,
                    "company": carrier,
                },
                "line_items_by_fulfillment_order": [
                    {"fulfillment_order_id": fo["id"]} for fo in open_fo
                ],
            }
        }
        resp = await client.post(f"{base_url}/fulfillments.json", json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            raise ShopifyOrderError(
                f"Could not mark Shopify order {shopify_order_id} fulfilled "
                f"({resp.status_code}): {resp.text[:300]}"
            )
