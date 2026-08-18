"""
GET /admin/revenue/shopify

Fetches orders from all 4 bridge/router stores (MPC, FRT Chek, ONEPEPSCHECK,
TWE Chek) in parallel via Shopify Admin API, normalizes them to the same shape
as the DB Order model so the existing admin-dashboard `buildRevenue` logic can
filter and sum them the same way as Interac/Crypto orders.
"""
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query

from config import settings
from routes.auth_routes import require_admin

router = APIRouter(
    prefix="/admin/revenue",
    tags=["admin-revenue"],
    dependencies=[Depends(require_admin)],
)
logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2024-07"


def _get_router_stores() -> list[dict]:
    """Mirror of the bridge worker's getCheckoutStores() — 4 bridge stores."""
    stores = [
        {"id": "mpc",     "name": "MPC",          "shop": settings.MPC_CHECKOUT_SHOP,  "token": settings.MPC_CHECKOUT_TOKEN},
        {"id": "store_1", "name": "FRT Chek",     "shop": settings.STORE_1_SHOP,       "token": settings.STORE_1_TOKEN},
    ]
    return [s for s in stores if s["shop"] and s["token"]]


async def _fetch_store_orders(
    client: httpx.AsyncClient,
    store: dict,
    created_at_min: Optional[str],
    created_at_max: Optional[str],
) -> list[dict]:
    """
    Fetch all orders for one Shopify store. Handles pagination via Link header.
    Returns normalized order dicts matching the admin dashboard shape.
    """
    url     = f"https://{store['shop']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    headers = {"X-Shopify-Access-Token": store["token"]}
    params: dict = {
        "status":           "any",
        "financial_status": "paid",
        "limit":            250,
    }
    if created_at_min:
        params["created_at_min"] = created_at_min
    if created_at_max:
        params["created_at_max"] = created_at_max

    collected: list[dict] = []
    pages_fetched = 0
    MAX_PAGES = 10     # 10 * 250 = 2500 orders per store — safety cap

    current_url    = url
    current_params = params

    try:
        while current_url and pages_fetched < MAX_PAGES:
            resp = await client.get(current_url, headers=headers, params=current_params, timeout=20.0)
            if resp.status_code != 200:
                logger.warning(f"Shopify API error for {store['name']}: {resp.status_code} — {resp.text[:200]}")
                break

            data = resp.json()
            orders = data.get("orders", [])
            for o in orders:
                total_price  = float(o.get("total_price") or 0)
                refund_total = 0.0
                for refund in (o.get("refunds") or []):
                    for rt in (refund.get("transactions") or []):
                        refund_total += float(rt.get("amount") or 0)
                net_total = max(0.0, total_price - refund_total)

                is_cancelled = bool(o.get("cancelled_at"))
                fin_status   = o.get("financial_status") or ""
                ful_status   = o.get("fulfillment_status") or "unfulfilled"

                collected.append({
                    "id":                str(o.get("id", "")),
                    "orderNumber":       o.get("name", ""),
                    "total":             f"{total_price:.2f}",
                    "netTotal":          f"{net_total:.2f}",
                    "refundedAmount":    f"{refund_total:.2f}",
                    "currency":          o.get("currency", "CAD"),
                    "paymentMethod":     "shopify",
                    "paymentStatus":     "paid" if fin_status == "paid" else (fin_status or "unknown"),
                    "financialStatus":   fin_status,
                    "fulfillmentStatus": ful_status,
                    "cancelled":         is_cancelled,
                    "createdAt":         o.get("created_at", "").replace("Z", "") if o.get("created_at") else "",
                    "paidAt":            o.get("processed_at", "").replace("Z", "") if o.get("processed_at") else (o.get("created_at", "").replace("Z", "") if o.get("created_at") else ""),
                    "email":             o.get("email", ""),
                    "storeName":         store["name"],
                    "sourceDomain":      store["shop"],
                    "storeId":           store["id"],
                    "cancelledAt":       o.get("cancelled_at"),
                })
            pages_fetched += 1

            # Shopify uses Link header for pagination
            link_header = resp.headers.get("link", "")
            next_url = None
            if 'rel="next"' in link_header:
                # Parse: <https://.../orders.json?page_info=xxx>; rel="next"
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                        break
            current_url    = next_url
            current_params = None     # next URL already contains page_info, no extra params

    except Exception as e:
        logger.exception(f"Failed to fetch orders from {store['name']}: {e}")

    return collected


@router.get("/shopify")
async def get_shopify_revenue_orders(
    from_date: Optional[str] = Query(None, description="ISO date, e.g. 2026-04-11"),
    to_date:   Optional[str] = Query(None, description="ISO date, e.g. 2026-04-18"),
    currency:  Optional[str] = Query(None, description="CAD or USD"),
) -> list[dict]:
    """
    Returns all orders from all router stores in normalized shape.
    The admin dashboard JS filters/sums these alongside DB orders.

    Date filtering is applied server-side via Shopify's created_at_min/max
    to reduce the response size.
    """
    stores = _get_router_stores()
    if not stores:
        logger.warning("No router stores configured — returning empty list")
        return []

    # Convert ISO date strings to ISO datetime for Shopify
    created_at_min = f"{from_date}T00:00:00Z" if from_date else None
    created_at_max = f"{to_date}T23:59:59Z"   if to_date   else None

    async with httpx.AsyncClient() as client:
        tasks = [_fetch_store_orders(client, s, created_at_min, created_at_max) for s in stores]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_orders: list[dict] = []
    for store, result in zip(stores, results):
        if isinstance(result, Exception):
            logger.error(f"Store {store['name']} fetch raised: {result}")
            continue
        all_orders.extend(result)
    
    if currency:
        cur_u = currency.upper()
        all_orders = [o for o in all_orders if (o.get("currency") or "").upper() == cur_u]

    return all_orders