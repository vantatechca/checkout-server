"""
Internal admin endpoints (should be behind IP whitelist in Nginx, not public).

GET  /admin/orders              → list orders with filters
GET  /admin/orders/{id}         → order detail
POST /admin/orders/{id}/mark-paid   → manually mark any order paid (creates a Shopify order)
POST /admin/orders/{id}/shipping/rates     → live Shippo rate quote for an order (any status)
POST /admin/orders/{id}/shipping/mark-paid-and-buy-label → mark paid (no Shopify order) + buy the quoted label, in one action
POST /admin/orders/{id}/shipping/buy-label → purchase a Shippo shipping label for an already-paid order
POST /admin/interac/match       → manually match an unmatched Interac payment
GET  /admin/interac/unmatched   → list Interac payments needing manual review
GET  /admin/brands              → list brands
POST /admin/brands              → create brand
PUT  /admin/brands/{id}         → update brand
"""
import asyncio
import json
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, desc, or_ as _sa_or, and_ as _sa_and, cast as _sa_cast, String as _sa_String

from database import get_db
from models.order import Order, InteracPayment, ZellePayment, CryptoInvoice, NowPaymentsInvoice, PaymentStatus, PaymentMethod
from models.brand import Brand
from models.admin_activity import AdminActivity
from routes.auth_routes import require_admin, require_write_access, get_redis
from config import settings


# "Today" for dashboard stats means the Eastern calendar day, not the UTC
# one. Using zoneinfo (not a fixed offset) so EST/EDT transitions are
# handled automatically.
_BUSINESS_TZ = ZoneInfo("America/New_York")


def _today_start_utc() -> datetime:
    """
    Midnight in America/New_York, expressed as the equivalent UTC instant —
    for comparison against created_at/paid_at, which are stored as naive
    datetimes representing true UTC (confirmed: the DB server's system
    timezone is UTC and MariaDB's NOW() == UTC_TIMESTAMP() exactly).
    """
    now_business = datetime.now(_BUSINESS_TZ)
    midnight_business = now_business.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_business.astimezone(timezone.utc)


async def _cache_get(cache_key: str):
    """
    Short-TTL response cache for hot, frequently-polled aggregate endpoints
    (stats, monitoring health) where every open dashboard tab re-triggers the
    same expensive query every 30s — this lets N concurrent tabs/admins share
    one computed result instead of each hitting the DB independently.

    Returns None on a cache miss OR if Redis is unavailable — callers treat
    both the same way (compute fresh), so a Redis hiccup degrades to
    "always fresh" instead of breaking the dashboard.
    """
    try:
        r = await get_redis()
        cached = await r.get(cache_key)
        return json.loads(cached) if cached is not None else None
    except Exception as e:
        logger.warning(f"[cache] Redis read failed for {cache_key}, computing live: {e}")
        return None


async def _cache_set(cache_key: str, value, ttl: int) -> None:
    try:
        r = await get_redis()
        await r.set(cache_key, json.dumps(value), ex=ttl)
    except Exception as e:
        logger.warning(f"[cache] Redis write failed for {cache_key}: {e}")


async def _cached_live_check(cache_key: str, check_fn, ttl: int = 300) -> Optional[bool]:
    """
    Like _cache_get/_cache_set but for a live external-API key-validity
    ping, with a much longer TTL (default 5min) than the 8s used for the
    main monitoring snapshot — monitoring_health is polled every 30s while
    any admin has the Dashboard tab open, and a key's validity doesn't
    change minute-to-minute, so without this a live ping would fire on
    that same hot cadence instead of ~once per 5 minutes.

    check_fn() returns True (confirmed working), False (confirmed broken —
    401/403), or None (couldn't tell — network hiccup, unexpected status).
    Wrapped in {"live": ...} so a cached None is distinguishable from a
    cache miss (both would otherwise decode to plain None via _cache_get).
    """
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached.get("live")
    result = await check_fn()
    await _cache_set(cache_key, {"live": result}, ttl=ttl)
    return result


async def _shopify_live_status(store_domain: str, api_token: str, cache_suffix: str) -> Optional[bool]:
    """True = key confirmed working, False = confirmed bad (401/403), None
    = not configured or couldn't tell (network issue) — callers should only
    treat an explicit False as "broken", never downgrade status on None."""
    if not store_domain or not api_token:
        return None

    async def _ping():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://{store_domain}/admin/api/2024-07/shop.json",
                    headers={"X-Shopify-Access-Token": api_token},
                )
            if resp.status_code == 200:
                return True
            if resp.status_code in (401, 403):
                return False
            return None
        except Exception:
            return None

    return await _cached_live_check(f"admin:health:live:shopify_{cache_suffix}", _ping)


async def _shippo_live_status(api_token: str) -> Optional[bool]:
    """Same True/False/None contract as _shopify_live_status above."""
    if not api_token:
        return None

    async def _ping():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.goshippo.com/carrier_accounts/",
                    headers={"Authorization": f"ShippoToken {api_token}"},
                )
            if resp.status_code == 200:
                return True
            if resp.status_code in (401, 403):
                return False
            return None
        except Exception:
            return None

    return await _cached_live_check("admin:health:live:shippo", _ping)


# Card `payment_ref` prefixes that indicate an asynchronous flow — the order
# legitimately sits in `pending` while it waits for an external event:
#   pay_  → pymtz card processor   (admin manually marks paid)
#   hr:   → Highriskify direct API (Transak/MoonPay webhook fires)
#   wc:   → WordPress onramp plugin (WC webhook fires)
# Stripe / Helcim card orders are SYNCHRONOUS — they pay-on-submit
# and never legitimately sit in `pending`, so they stay excluded.
DELAYED_CARD_REF_PREFIXES = ("pay_", "hr:", "wc:")


def _is_delayed_card():
    """
    SQLAlchemy OR clause: True when the order is a card-typed order whose
    `payment_ref` indicates an asynchronous path (pymtz / Highriskify /
    WP onramp). Used by every admin tab filter that needs to include
    "legitimately pending card orders" alongside non-card payment methods.
    """
    return _sa_or(*[Order.payment_ref.like(p + "%") for p in DELAYED_CARD_REF_PREFIXES])


def _is_delayed_card_py(method, ref) -> bool:
    """Python-side equivalent of _is_delayed_card() for in-memory row checks."""
    if method != PaymentMethod.card:
        return False
    r = (ref or "")
    return any(r.startswith(p) for p in DELAYED_CARD_REF_PREFIXES)


async def log_admin_activity(
    db: AsyncSession,
    request: Optional[Request],
    *,
    action: str,
    target_type: str = "",
    target_id: str = "",
    details: str = "",
) -> None:
    """
    Record an admin action to the audit log. Safe to call from any admin
    endpoint; failures are swallowed (logging an audit row must never break
    the actual action).
    """
    try:
        ip = ""
        if request and request.client:
            ip = request.client.host or ""
        row = AdminActivity(
            admin_user  = settings.ADMIN_USERNAME or "admin",
            action      = action,
            target_type = target_type or None,
            target_id   = target_id   or None,
            details     = (details or "")[:1000] or None,
            ip_address  = ip or None,
        )
        db.add(row)
        await db.commit()
    except Exception as e:
        logger.warning(f"audit-log write failed for action={action}: {e}")

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
logger = logging.getLogger(__name__)


# ─── Orders ──────────────────────────────────────────────────────────────────

@router.get("/orders")
async def list_orders(
    status:      Optional[str] = Query(None),
    method:      Optional[str] = Query(None),
    brand_id:    Optional[int] = Query(None),
    email:       Optional[str] = Query(None),
    currency:    Optional[str] = Query(None),    # "CAD" or "USD"
    awaiting:    Optional[str] = Query(None),
    not_emailed: Optional[str] = Query(None),
    abandoned:   Optional[str] = Query(None),
    failed:      Optional[str] = Query(None),
    shipping:    Optional[str] = Query(None),
    limit:       int           = Query(50, le=100000),   # high cap so CSV export can pull the full dataset
    offset:      int           = Query(0),
    db: AsyncSession = Depends(get_db),
):
    q = select(Order).order_by(desc(Order.created_at))

    if status:
        q = q.where(Order.payment_status == status)
        if status == "pending":
            # Include orders that legitimately sit in pending: non-card methods
            # (interac/zelle/crypto/altcoin) plus the async card paths defined
            # in DELAYED_CARD_REF_PREFIXES (pymtz / Highriskify / WP onramp).
            # Synchronous card paths (Stripe/Helcim) stay excluded — they
            # never legitimately sit in `pending`.
            q = q.where(
                _sa_or(
                    Order.payment_method != PaymentMethod.card,
                    _is_delayed_card(),
                )
            )
    if method:
        q = q.where(Order.payment_method == method)
    if brand_id:
        q = q.where(Order.brand_id == brand_id)
    if email:
        q = q.where(Order.email.ilike(f"%{email}%"))
    if currency:
        q = q.where(Order.currency == currency.upper())

    # Pending tab — orders we haven't emailed yet AND aren't underpaid
    if not_emailed == "yes":
        q = q.outerjoin(InteracPayment, InteracPayment.order_id == Order.id) \
             .outerjoin(ZellePayment,   ZellePayment.order_id   == Order.id) \
             .where(
                ((Order.customer_emails_sent == 0) | (Order.customer_emails_sent.is_(None))) &
                ((InteracPayment.status.is_(None)) | (InteracPayment.status != "underpaid")) &
                ((ZellePayment.status.is_(None))   | (ZellePayment.status   != "underpaid"))
             )

    # "Awaiting Payment" merged tab: pending+emailed OR underpaid Interac/Zelle
    if awaiting == "yes":
        q = q.outerjoin(InteracPayment,    InteracPayment.order_id    == Order.id) \
                .outerjoin(ZellePayment,      ZellePayment.order_id      == Order.id) \
                .outerjoin(NowPaymentsInvoice, NowPaymentsInvoice.order_id == Order.id) \
                .where(
                (
                    (Order.payment_status == PaymentStatus.pending) &
                    ((Order.payment_method != PaymentMethod.card) | _is_delayed_card()) &
                    (Order.customer_emails_sent > 0)
                ) |
                (InteracPayment.status == "underpaid") |
                (ZellePayment.status   == "underpaid") |
                (NowPaymentsInvoice.status == "underpaid")
                )

    # "Abandoned" tab — customer auto-saved their info but never clicked Place Order.
    # Identified by:
    #   - status is still pending
    #   - we have customer info filled (email + last_name)
    #   - we never sent them an email (emails are sent on Place Order)
    #   - no InteracPayment/ZellePayment row exists (those are created on Place Order)
    # These are orders we may need to recover — customer might have paid externally
    # without finishing the form, or simply abandoned with intent.
    if abandoned == "yes":
        q = q.outerjoin(InteracPayment, InteracPayment.order_id == Order.id) \
             .outerjoin(ZellePayment,   ZellePayment.order_id   == Order.id) \
             .where(
                (Order.payment_status == PaymentStatus.pending) &
                (Order.email != "") &
                (Order.email.is_not(None)) &
                (Order.last_name != "") &
                (Order.last_name.is_not(None)) &
                ((Order.customer_emails_sent == 0) | (Order.customer_emails_sent.is_(None))) &
                (InteracPayment.id.is_(None)) &
                (ZellePayment.id.is_(None))
             )

    # "Shipping" tab — a record of orders fulfilled through the combined
    # Shippo flow specifically (Pending tab's "Mark Paid (Shippo)" button,
    # which marks paid AND buys the label in one action). Requires BOTH
    # paid_via_shippo and a tracking number — a Shopify-paid order that
    # separately got a label bought for it does not belong here.
    if shipping == "yes":
        q = q.where(
            Order.paid_via_shippo == True,  # noqa: E712
            Order.tracking_number.is_not(None),
        )

    # Failed tab — payment never succeeded; admin can attempt recovery
    if failed == "yes":
        q = q.where(
            Order.payment_status.in_([PaymentStatus.failed, PaymentStatus.expired])
        )

    # Eager-load payment relations so we can show shortfall info on rows
    q = q.options(
        selectinload(Order.interac_payment),
        selectinload(Order.zelle_payment),
        selectinload(Order.crypto_invoice),
        selectinload(Order.nowpayments_invoice),
    )

    result = await db.execute(q.limit(limit).offset(offset))
    orders = result.scalars().unique().all()

    return [_build_order_row(o) for o in orders]


def _build_order_row(o: Order) -> dict:
    """
    Shared per-order dict builder for /admin/orders and /admin/orders/search —
    keeps the underpaid/abandoned/unmarked/isV2 derived fields identical
    between both endpoints instead of duplicating the logic.
    """
    d = o.to_dict()
    if o.interac_payment and o.interac_payment.status == "underpaid":
        d["receivedAmount"]  = float(o.interac_payment.received_amount or 0)
        d["underpaidMethod"] = "interac"
    elif o.zelle_payment and o.zelle_payment.status == "underpaid":
        d["receivedAmount"]  = float(o.zelle_payment.received_amount or 0)
        d["underpaidMethod"] = "zelle"

    elif o.crypto_invoice and o.crypto_invoice.status == "Underpaid":
        d["receivedAmount"]  = float(o.crypto_invoice.received_fiat or 0)
        d["underpaidMethod"] = "crypto"

    elif o.nowpayments_invoice and o.nowpayments_invoice.status == "underpaid":
        d["receivedAmount"]  = float(o.nowpayments_invoice.received_fiat or 0)
        d["underpaidMethod"] = "altcoin"

    # Flag abandoned orders (customer info saved, never clicked Place Order)
    if (
        o.payment_status == PaymentStatus.pending
        and o.email and o.last_name
        and (o.customer_emails_sent or 0) == 0
        and not o.interac_payment
        and not o.zelle_payment
        # Async card paths (pymtz / Highriskify / WP onramp) sit in
        # pending legitimately while waiting on admin / webhook — not abandoned.
        and not _is_delayed_card_py(o.payment_method, o.payment_ref)
    ):
        d["isAbandoned"] = True

    # Flag orders that were previously paid then reverted via unmark-paid.
    # The audit prefix is set in `unmark_order_paid` — see this file.
    notes = o.payment_notes or ""
    if notes.startswith("[unmark-paid @ "):
        d["wasUnmarked"] = True
        # Best-effort extract the timestamp inside `[unmark-paid @ <iso>]`
        try:
            end = notes.index("]")
            d["unmarkedAt"] = notes[len("[unmark-paid @ "):end].strip()
        except ValueError:
            pass

    # "NEW store" pill — derived (not stored on the order). True iff the
    # order's source_domain matches the v2 store list at
    # data/checkout_v2_stores.txt. Cached + mtime-invalidated upstream.
    from main import _is_v2_store
    d["isV2"] = _is_v2_store(o.source_domain or "")

    return d


@router.get("/orders/search")
async def search_orders(
    q: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
):
    """
    Direct DB lookup across the FULL orders table (no date/limit window) —
    for finding an order older than the bounded default fetch the dashboard
    normally loads. Matches order ID, email, first/last name, payment_ref,
    or order total (amount) — same fields the client-side search box
    matches on.
    """
    term = q.strip()
    if not term:
        return []

    like = f"%{term}%"
    query = (
        select(Order)
        .where(
            _sa_or(
                Order.id.ilike(like),
                Order.email.ilike(like),
                Order.first_name.ilike(like),
                Order.last_name.ilike(like),
                Order.payment_ref.ilike(like),
                _sa_cast(Order.total, _sa_String).ilike(like),
            )
        )
        .order_by(desc(Order.created_at))
        .limit(200)
        .options(
            selectinload(Order.interac_payment),
            selectinload(Order.zelle_payment),
            selectinload(Order.crypto_invoice),
            selectinload(Order.nowpayments_invoice),
        )
    )
    result = await db.execute(query)
    orders = result.scalars().unique().all()
    return [_build_order_row(o) for o in orders]


@router.get("/orders/stats")
async def order_stats(
    currency: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Server-side aggregate counts for dashboard stat cards + tab badges.
    Accurate at any scale — doesn't fetch full rows.

    Cached in Redis for 8s (see _cache_get/_cache_set) — this is polled every
    30s by every open dashboard tab, so the cache lets concurrent admins
    share one computed result instead of each re-running the full query set.
    """
    cache_key = f"admin:orders:stats:{(currency or 'all').upper()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    from sqlalchemy import func as sa_func, and_, or_, case
    from datetime import datetime, timezone

    base_filter = []
    if currency:
        base_filter.append(Order.currency == currency.upper())

    # Mirrors list_orders' exact `status=pending&not_emailed=yes` filter (the
    # Pending tab's real query) — must also exclude underpaid Interac/Zelle
    # orders like that query does, or this badge overcounts vs. what the tab
    # actually lists (an underpaid-but-unemailed order belongs in Awaiting).
    pending_q = (
        select(sa_func.count())
        .select_from(Order)
        .outerjoin(InteracPayment, InteracPayment.order_id == Order.id)
        .outerjoin(ZellePayment,   ZellePayment.order_id   == Order.id)
        .where(
            and_(
                *base_filter,
                Order.payment_status == PaymentStatus.pending,
                or_(
                    Order.payment_method != PaymentMethod.card,
                    _is_delayed_card(),    # pymtz / Highriskify / WP onramp
                ),
                or_(Order.customer_emails_sent == 0, Order.customer_emails_sent.is_(None)),
                or_(InteracPayment.status.is_(None), InteracPayment.status != "underpaid"),
                or_(ZellePayment.status.is_(None),   ZellePayment.status   != "underpaid"),
            )
        )
    )

    # Mirrors list_orders' exact `shipping=yes` filter (the Shipping tab's
    # real query): orders fulfilled through the combined Shippo flow only.
    shipping_q = select(sa_func.count()).select_from(Order).where(
        and_(
            *base_filter,
            Order.paid_via_shippo == True,  # noqa: E712
            Order.tracking_number.is_not(None),
        )
    )

    start_today = _today_start_utc()
    paid_q = select(sa_func.count()).select_from(Order).where(
        and_(*base_filter, Order.payment_status == PaymentStatus.paid)
    )
    paid_today_q = select(sa_func.count()).select_from(Order).where(
        and_(*base_filter, Order.payment_status == PaymentStatus.paid, Order.paid_at >= start_today)
    )

    all_q = select(sa_func.count()).select_from(Order).where(
        and_(
            *base_filter,
            or_(
                Order.payment_method != PaymentMethod.card,
                Order.payment_status != PaymentStatus.pending,
                _is_delayed_card(),    # pymtz / Highriskify / WP onramp pending cards count
            ),
        )
    )

    underpaid_q = (
        select(sa_func.count())
        .select_from(Order)
        .outerjoin(InteracPayment,    InteracPayment.order_id    == Order.id)
        .outerjoin(ZellePayment,      ZellePayment.order_id      == Order.id)
        .outerjoin(CryptoInvoice,     CryptoInvoice.order_id     == Order.id)
        .outerjoin(NowPaymentsInvoice, NowPaymentsInvoice.order_id == Order.id)
        .where(
            and_(
                *base_filter,
                or_(
                    InteracPayment.status == "underpaid",
                    ZellePayment.status   == "underpaid",
                    CryptoInvoice.status  == "Underpaid",
                    NowPaymentsInvoice.status == "underpaid",
                ),
           )
        )
    )

    # Mirrors list_orders' exact `awaiting=yes` filter (the Awaiting tab's
    # real query): pending-and-already-emailed OR underpaid via any method.
    # Previously this badge was computed client-side as pending + underpaid,
    # which double-counted an underpaid-but-unemailed order (it's already
    # inside `pending`) and didn't require the emailed flag the real tab does.
    awaiting_q = (
        select(sa_func.count())
        .select_from(Order)
        .outerjoin(InteracPayment,     InteracPayment.order_id     == Order.id)
        .outerjoin(ZellePayment,       ZellePayment.order_id       == Order.id)
        .outerjoin(NowPaymentsInvoice, NowPaymentsInvoice.order_id == Order.id)
        .where(
            and_(
                *base_filter,
                or_(
                    and_(
                        Order.payment_status == PaymentStatus.pending,
                        or_(Order.payment_method != PaymentMethod.card, _is_delayed_card()),
                        Order.customer_emails_sent > 0,
                    ),
                    InteracPayment.status == "underpaid",
                    ZellePayment.status   == "underpaid",
                    NowPaymentsInvoice.status == "underpaid",
                ),
            )
        )
    )

    # Failed = failed OR expired — both are "recoverable" terminal states
    failed_q = select(sa_func.count()).select_from(Order).where(
        and_(
            *base_filter,
            Order.payment_status.in_([PaymentStatus.failed, PaymentStatus.expired]),
        )
    )

    # Today's revenue, grouped by currency — NOT summed across currencies.
    # A raw SUM(Order.total) across CAD+USD rows would add two different
    # currencies together as if $1 CAD == $1 USD, producing a number that
    # isn't a real amount in either currency. Only safe to collapse to one
    # number when `currency` was explicitly filtered (base_filter already
    # restricts to a single currency in that case).
    #
    # Group/sum by the SETTLED amount+currency when present (pymtz/WPay HPP/
    # WPay 2D convert CAD carts to USD at charge time — settled_amount/
    # settled_currency record what was actually collected). Falling back to
    # Order.total/Order.currency here would count a WPay order's pre-conversion
    # CAD cart total as CAD revenue even though the card was charged in USD —
    # exactly the mismatch that made the order list (which already prefers
    # settledAmount/settledCurrency) disagree with this card.
    revenue_amount_expr   = sa_func.coalesce(Order.settled_amount, Order.total)
    revenue_currency_expr = sa_func.coalesce(Order.settled_currency, Order.currency)
    revenue_today_by_currency_q = (
        select(revenue_currency_expr, sa_func.coalesce(sa_func.sum(revenue_amount_expr), 0))
        .where(and_(*base_filter, Order.payment_status == PaymentStatus.paid, Order.paid_at >= start_today))
        .group_by(revenue_currency_expr)
    )

    pending_count    = (await db.execute(pending_q)).scalar_one()
    shipping_count   = (await db.execute(shipping_q)).scalar_one()
    awaiting_count   = (await db.execute(awaiting_q)).scalar_one()
    paid_count       = (await db.execute(paid_q)).scalar_one()
    paid_today_count = (await db.execute(paid_today_q)).scalar_one()
    all_count        = (await db.execute(all_q)).scalar_one()
    underpaid_count  = (await db.execute(underpaid_q)).scalar_one()
    failed_count     = (await db.execute(failed_q)).scalar_one()

    revenue_today_by_currency = {
        (cur or "UNKNOWN"): float(amt or 0)
        for cur, amt in (await db.execute(revenue_today_by_currency_q)).all()
    }
    # Legacy single-number field — only meaningful when `currency` was
    # explicitly filtered (then there's only one key). Kept so any existing
    # caller that only reads `revenueToday` doesn't break; the dashboard UI
    # now prefers `revenueTodayByCurrency` when more than one currency is present.
    revenue_today = sum(revenue_today_by_currency.values())

    # Device breakdown across all orders (derived from stored user-agents).
    # Computed server-side via a single grouped COUNT — mirrors the priority
    # order of models.order._classify_device() exactly (Tablet checked before
    # Mobile, since Android tablets also match the generic "android" keyword)
    # instead of pulling every user_agent row into Python to classify one by one.
    ua = sa_func.lower(Order.user_agent)
    device_case = case(
        (or_(Order.user_agent.is_(None), Order.user_agent == ""), "Unknown"),
        (
            or_(
                ua.like("%ipad%"),
                ua.like("%tablet%"),
                and_(ua.like("%android%"), ~ua.like("%mobile%")),
            ),
            "Tablet",
        ),
        (
            or_(
                ua.like("%mobi%"),
                ua.like("%iphone%"),
                ua.like("%ipod%"),
                ua.like("%android%"),
                ua.like("%windows phone%"),
                ua.like("%blackberry%"),
            ),
            "Mobile",
        ),
        else_="Desktop",
    )
    device_q = select(device_case.label("device"), sa_func.count()).select_from(Order)
    if base_filter:
        device_q = device_q.where(and_(*base_filter))
    device_q = device_q.group_by(device_case)
    device_counts = {"Mobile": 0, "Desktop": 0, "Tablet": 0, "Unknown": 0}
    for device, cnt in (await db.execute(device_q)).all():
        device_counts[device] = int(cnt)
    device_total = sum(device_counts.values()) or 1
    device_pct = {k: round(v / device_total * 100) for k, v in device_counts.items()}

    result = {
        "pending":         pending_count,
        "shipping":        shipping_count,
        "awaiting":        awaiting_count,
        "paid":            paid_count,
        "paidToday":       paid_today_count,
        "all":             all_count,
        "underpaid":       underpaid_count,
        "failed":          failed_count,
        "revenueToday":    revenue_today,
        "revenueTodayByCurrency": revenue_today_by_currency,
        "deviceCounts":    device_counts,
        "devicePct":       device_pct,
    }
    await _cache_set(cache_key, result, ttl=8)
    return result

@router.get("/orders/{order_id}")
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    data = order.to_dict()
    data["items"] = [
        {
            "title":   item.title,
            "variant": item.variant,
            "qty":     item.qty,
            "price":   float(item.price),
            "total":   float(item.total),
        }
        for item in order.items
    ]
    return data


class MarkPaidRequest(BaseModel):
    notes: Optional[str] = None


class CustomerEmailOverride(BaseModel):
    """Optional fields that let admin override the auto-generated email."""
    custom_subject: Optional[str] = None
    custom_html:    Optional[str] = None
    custom_text:    Optional[str] = None


class MarkUnderpaidRequest(CustomerEmailOverride):
    received_amount: float
    notes: Optional[str] = None
    send_email: bool = True


class SendReminderRequest(CustomerEmailOverride):
    received_amount: float = 0  # 0 = standard reminder; > 0 = partial payment, flags underpaid
    notes: Optional[str] = None


async def _resolve_payment_email(order, db) -> tuple[str, str]:
    """Returns (payment_email, accent_color) for the order's brand.
    Payment emails always come from .env — single source of truth.
    Only the brand's accent color is read from DB.
    """
    brand = (await db.execute(
        select(Brand).where(Brand.id == order.brand_id)
    )).scalar_one_or_none()

    accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"

    if order.payment_method == PaymentMethod.interac:
        email = settings.INTERAC_DEFAULT_EMAIL
    else:  # zelle
        email = settings.ZELLE_DEFAULT_EMAIL

    return email, accent


def _apply_overrides(template: dict, override: CustomerEmailOverride) -> dict:
    """Merges admin overrides into the default template dict."""
    from services.email import text_to_html
    out = dict(template)
    if override.custom_subject:
        out["subject"] = override.custom_subject
    if override.custom_html:
        out["html"] = override.custom_html
        if override.custom_text:
            out["text"] = override.custom_text
    elif override.custom_text:
        out["html"] = text_to_html(override.custom_text)
        out["text"] = override.custom_text
    return out


async def _apply_paid_status(
    db: AsyncSession,
    order_id: str,
    notes: Optional[str],
    default_note: str,
) -> Order:
    """
    Shared core of "mark this pending order as paid": looks up the order,
    rejects if missing/already paid, flips payment_status/paid_at, and
    syncs the Interac/Zelle payment record if applicable (clears underpaid
    flag, tops up received_amount). Does NOT call finalize_paid_order —
    callers decide Shopify/email/affiliate behavior themselves. Used by
    both the normal mark-paid endpoint and the Shipping tab's lightweight
    (no-Shopify) one.
    """
    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.interac_payment))
        .options(selectinload(Order.zelle_payment))
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status == PaymentStatus.paid:
        raise HTTPException(400, "Order already marked as paid")

    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.now(timezone.utc)
    order.payment_notes  = notes or default_note

    # If Interac, also update interac_payment record (clears underpaid flag if it was set)
    if order.payment_method == PaymentMethod.interac and order.interac_payment:
        order.interac_payment.status          = "manual"
        order.interac_payment.matched_at      = datetime.now(timezone.utc)
        # If was underpaid, set received_amount to the full total now that balance is in
        if order.interac_payment.received_amount is not None:
            order.interac_payment.received_amount = order.total

    # If Zelle, also update zelle_payment record (clears underpaid flag if it was set)
    if order.payment_method == PaymentMethod.zelle and order.zelle_payment:
        order.zelle_payment.status            = "manual"
        order.zelle_payment.matched_at        = datetime.now(timezone.utc)
        if order.zelle_payment.received_amount is not None:
            order.zelle_payment.received_amount = order.total

    await db.commit()
    return order


@router.post("/orders/{order_id}/mark-paid", dependencies=[Depends(require_write_access)])
async def mark_order_paid(
    order_id: str,
    body: MarkPaidRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _apply_paid_status(db, order_id, body.notes, "Manually marked paid by admin")
    await log_admin_activity(
        db, request,
        action="mark_paid", target_type="order", target_id=order_id,
        details=(body.notes or "no note")[:200],
    )

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()

    from services.order_finalize import finalize_paid_order
    result_info = await finalize_paid_order(order, db, label="admin-mark-paid")
    resp = {"success": True, "orderId": order_id}
    if result_info.get("shopify_error"):
        resp["shopifyError"] = result_info["shopify_error"]
    if result_info.get("affiliate_error"):
        resp["affiliateError"] = result_info["affiliate_error"]
    return resp


# ─── Shipping (Shippo) ──────────────────────────────────────────────────────
#
# The "Shipping" tab in the admin dashboard lists pending orders and lets an
# admin mark one paid WITHOUT creating a Shopify order — that's the only
# behavioral difference from the normal mark-paid endpoint above. Every
# other path to "paid" (Pending tab, Interac/Zelle match, webhooks) still
# creates a Shopify order exactly as before; this is a deliberately separate,
# additive endpoint, not a change to the existing one.

def _shippo_order_eligible(order) -> bool:
    """Shippo is scoped to CAD Interac orders only for now (explicitly
    excludes crypto/BTC even on the off chance one is CAD-denominated) —
    a deliberate, narrower business restriction on top of the technical
    CA/US ship-from support in services/shippo.py. Update here if that
    scope changes."""
    return (
        (order.currency or "").upper() == "CAD"
        and order.payment_method == PaymentMethod.interac
    )

class ShippingFromAddress(BaseModel):
    name:    str = ""
    street1: str = ""
    street2: str = ""
    city:    str = ""
    state:   str = ""
    zip:     str = ""
    country: str = ""
    phone:   str = ""


class ShippingRatesRequest(BaseModel):
    weight_oz: float
    length_in: float
    width_in:  float
    height_in: float
    from_address: Optional[ShippingFromAddress] = None


class ShippingBuyLabelRequest(BaseModel):
    rate_id:   str
    carrier:   str
    weight_oz: float
    length_in: float
    width_in:  float
    height_in: float


class ShippingMarkPaidBuyLabelRequest(BaseModel):
    notes:     Optional[str] = None
    rate_id:   str
    carrier:   str
    weight_oz: float
    length_in: float
    width_in:  float
    height_in: float


@router.get("/shipping/default-address", dependencies=[Depends(require_write_access)])
async def get_shipping_default_address_generic(currency: str = "CAD"):
    """Same shape as the per-order variant below, but for the bulk-shipping
    workflow, which applies one shared ship-from address + parcel across a
    whole batch instead of deriving it from a single order's currency."""
    from services.shippo import ShippoClient
    client = ShippoClient()
    return {
        "success": True,
        "address": client.default_from_address_for_currency(currency),
        "parcel":  client.default_parcel(),
    }


@router.get("/orders/{order_id}/shipping/default-address", dependencies=[Depends(require_write_access)])
async def get_shipping_default_address(
    order_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Returns the configured CA/US ship-from default for this order's
    currency plus the default parcel size, so the Buy Label form can prefill
    both — the admin can still edit every field before requesting rates."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    from services.shippo import ShippoClient
    client = ShippoClient()
    return {
        "success": True,
        "address": client.default_from_address(order),
        "parcel":  client.default_parcel(),
    }


@router.post("/orders/{order_id}/shipping/rates", dependencies=[Depends(require_write_access)])
async def get_shipping_rates(
    order_id: str,
    body: ShippingRatesRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")
    if not _shippo_order_eligible(order):
        raise HTTPException(400, "Shippo shipping labels are currently only available for CAD Interac orders")

    from services.shippo import ShippoClient, ShippoError
    try:
        rates = await ShippoClient().get_rates(
            order,
            weight_oz=body.weight_oz,
            length_in=body.length_in,
            width_in=body.width_in,
            height_in=body.height_in,
            from_address=body.from_address.dict() if body.from_address else None,
        )
    except ShippoError as e:
        # 400, not 502 — staging/prod sit behind Cloudflare, which replaces a
        # 502 response body with its own generic HTML error page instead of
        # passing through our JSON, so the real Shippo error never reaches
        # the frontend (surfaces there as a misleading "Network error").
        raise HTTPException(400, f"Could not fetch shipping rates: {e}")
    return {"success": True, "rates": rates}


@router.post("/orders/{order_id}/shipping/mark-paid-and-buy-label", dependencies=[Depends(require_write_access)])
async def mark_paid_and_buy_label(
    order_id: str,
    body: ShippingMarkPaidBuyLabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Pending tab's "Mark Paid (Shippo)" flow — marks the order paid WITHOUT
    creating a Shopify order, then immediately buys the label for the rate
    the admin already picked (via /shipping/rates). One admin action, two
    steps. Sets paid_via_shippo=True so this order (and only orders that
    complete this exact path) show up on the Shipping tab afterward.

    If the label purchase fails after the order is already marked paid, the
    order is left paid-but-unlabeled rather than rolled back — the affiliate
    webhook/email have already fired by that point too. It's still reachable
    via the Paid tab's Buy Shipping Label section as a fallback, so nothing
    about the purchase is lost, but it won't yet show on the Shipping tab.
    """
    precheck = await db.execute(select(Order).where(Order.id == order_id))
    precheck_order = precheck.scalar_one_or_none()
    if not precheck_order:
        raise HTTPException(404, "Order not found")
    if not _shippo_order_eligible(precheck_order):
        raise HTTPException(400, "Shippo shipping labels are currently only available for CAD Interac orders")

    await _apply_paid_status(
        db, order_id, body.notes,
        "Marked paid + label bought via Shippo (no Shopify order)",
    )

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()

    from services.order_finalize import finalize_paid_order
    finalize_info = await finalize_paid_order(order, db, label="shipping-mark-paid-buy-label", create_shopify=False)

    order.paid_via_shippo = True
    await db.commit()

    await log_admin_activity(
        db, request,
        action="shipping_mark_paid", target_type="order", target_id=order_id,
        details=(body.notes or "no note")[:200],
    )

    from services.shippo import ShippoClient, ShippoError
    try:
        label = await ShippoClient().buy_label(
            rate_id=body.rate_id,
            order_id=order_id,
            carrier=body.carrier,
        )
    except ShippoError as e:
        resp = {
            "success":    True,
            "orderId":    order_id,
            "paid":       True,
            "labelError": f"Order was marked paid, but the label purchase failed: {e}",
        }
        if finalize_info.get("affiliate_error"):
            resp["affiliateError"] = finalize_info["affiliate_error"]
        return resp

    order.tracking_number       = label["tracking_number"]
    order.tracking_url          = label["tracking_url"]
    order.carrier                = label["carrier"]
    order.label_url              = label["label_url"]
    order.shippo_transaction_id  = label["transaction_id"]
    order.shipped_at             = datetime.now(timezone.utc)
    order.package_weight_oz      = body.weight_oz
    order.package_length_in      = body.length_in
    order.package_width_in       = body.width_in
    order.package_height_in      = body.height_in
    await db.commit()

    await log_admin_activity(
        db, request,
        action="create_label", target_type="order", target_id=order_id,
        details=f"{label['carrier']} — {label['tracking_number']}"[:200],
    )

    resp = {"success": True, "orderId": order_id, "paid": True, **label}
    if finalize_info.get("affiliate_error"):
        resp["affiliateError"] = finalize_info["affiliate_error"]
    return resp


@router.post("/orders/{order_id}/shipping/buy-label", dependencies=[Depends(require_write_access)])
async def buy_shipping_label(
    order_id: str,
    body: ShippingBuyLabelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status != PaymentStatus.paid:
        raise HTTPException(400, "Order must be paid before buying a shipping label")
    if not _shippo_order_eligible(order):
        raise HTTPException(400, "Shippo shipping labels are currently only available for CAD Interac orders")

    from services.shippo import ShippoClient, ShippoError
    try:
        label = await ShippoClient().buy_label(
            rate_id=body.rate_id,
            order_id=order_id,
            carrier=body.carrier,
        )
    except ShippoError as e:
        raise HTTPException(400, f"Could not purchase shipping label: {e}")

    order.tracking_number       = label["tracking_number"]
    order.tracking_url          = label["tracking_url"]
    order.carrier                = label["carrier"]
    order.label_url              = label["label_url"]
    order.shippo_transaction_id  = label["transaction_id"]
    order.shipped_at             = datetime.now(timezone.utc)
    order.package_weight_oz      = body.weight_oz
    order.package_length_in      = body.length_in
    order.package_width_in       = body.width_in
    order.package_height_in      = body.height_in
    await db.commit()

    await log_admin_activity(
        db, request,
        action="create_label", target_type="order", target_id=order_id,
        details=f"{label['carrier']} — {label['tracking_number']}"[:200],
    )
    return {"success": True, "orderId": order_id, **label}


# ─── Bulk shipping labels ───────────────────────────────────────────────────
#
# Boss request: a single screen to buy many labels at once, covering both
# our own paid-but-unlabeled CAD Interac orders AND orders sitting
# unfulfilled on Shopify (100+ at a time there). One shared ship-from
# address + parcel size is used across the whole batch — that's the point
# of "bulk": same product, same packaging, same warehouse, not per-order
# editing. Local orders still go through the same CAD-Interac-only gate as
# the single-order flow (_shippo_order_eligible); Shopify orders have no
# such restriction since they're a separate paid-elsewhere workflow.

@router.get("/shipping/bulk-candidates", dependencies=[Depends(require_write_access)])
async def get_bulk_shipping_candidates(db: AsyncSession = Depends(get_db)):
    """
    Everything eligible for bulk labeling right now: our own paid-but-
    unlabeled CAD Interac orders, plus paid-but-unfulfilled orders pulled
    live from Shopify (CA + US stores, whichever are configured). The
    Shopify half is never persisted — read fresh every call, since
    Shopify's own fulfillment status is the source of truth for it.
    """
    result = await db.execute(
        select(Order)
        .where(
            Order.payment_status == PaymentStatus.paid,
            Order.payment_method == PaymentMethod.interac,
            Order.currency == "CAD",
            Order.tracking_number.is_(None),
        )
        .order_by(desc(Order.paid_at))
    )
    local = [
        {
            "ref":        o.id,
            "source":     "local",
            "firstName":  o.first_name or "",
            "lastName":   o.last_name or "",
            "email":      o.email or "",
            "address1":   o.address1 or "",
            "address2":   o.address2 or "",
            "city":       o.city or "",
            "province":   o.province or "",
            "postalCode": o.postal_code or "",
            "country":    o.country or "",
            "phone":      o.phone or "",
            "currency":   o.currency,
            "total":      float(o.total),
            "paidAt":     o.paid_at.isoformat() if o.paid_at else None,
        }
        for o in result.scalars().all()
    ]

    from services.shopify import list_unfulfilled_orders, ShopifyOrderError
    shopify: list[dict] = []
    shopify_errors: list[str] = []
    for store in ("CA", "US"):
        try:
            shopify.extend(await list_unfulfilled_orders(store))
        except ShopifyOrderError as e:
            shopify_errors.append(f"{store}: {e}")

    return {"success": True, "local": local, "shopify": shopify, "shopifyErrors": shopify_errors}


class BulkRatesRequest(BaseModel):
    refs:         list[str]
    from_address: ShippingFromAddress
    weight_oz:    float
    length_in:    float
    width_in:     float
    height_in:    float


@router.post("/shipping/bulk-rates", dependencies=[Depends(require_write_access)])
async def get_bulk_shipping_rates(
    body: BulkRatesRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Cheapest live rate for each selected ref (a local order id, or
    "shopify:CA:<id>" / "shopify:US:<id>"), using ONE shared ship-from
    address + parcel size for every ref in the batch. Read-only and free
    (Shippo doesn't charge for rate lookups) — safe to call as often as
    needed while comparing prices before actually buying anything.
    """
    from services.shippo import ShippoClient, ShippoError
    client = ShippoClient()
    from_addr = body.from_address.dict()

    local_refs    = [r for r in body.refs if not r.startswith("shopify:")]
    shopify_refs  = [r for r in body.refs if r.startswith("shopify:")]

    orders_by_id: dict = {}
    if local_refs:
        result = await db.execute(select(Order).where(Order.id.in_(local_refs)))
        orders_by_id = {o.id: o for o in result.scalars().all()}

    shopify_by_ref: dict = {}
    if shopify_refs:
        from services.shopify import list_unfulfilled_orders
        for store in {"CA", "US"}:
            for so in await list_unfulfilled_orders(store):
                shopify_by_ref[so["ref"]] = so

    async def rate_for(ref: str) -> dict:
        to_addr = None
        order = None
        if ref.startswith("shopify:"):
            so = shopify_by_ref.get(ref)
            if not so:
                return {"ref": ref, "error": "Shopify order not found (already fulfilled or removed since the list was loaded?)"}
            to_addr = {
                "name":    f"{so['firstName']} {so['lastName']}".strip(),
                "street1": so["address1"], "street2": so["address2"],
                "city":    so["city"],     "state":   so["province"],
                "zip":     so["postalCode"], "country": so["country"],
                "phone":   so["phone"],
            }
        else:
            order = orders_by_id.get(ref)
            if not order:
                return {"ref": ref, "error": "Order not found"}

        try:
            rates = await client.get_rates(
                order,
                weight_oz=body.weight_oz, length_in=body.length_in,
                width_in=body.width_in,   height_in=body.height_in,
                from_address=from_addr,
                to_address=to_addr,
            )
            return {"ref": ref, "cheapest": rates[0] if rates else None, "rateCount": len(rates)}
        except ShippoError as e:
            return {"ref": ref, "error": str(e)}

    results = await asyncio.gather(*(rate_for(r) for r in body.refs))
    return {"success": True, "results": results}


class BulkBuyItem(BaseModel):
    ref:      str
    rate_id:  str
    carrier:  str


class BulkBuyRequest(BaseModel):
    items:     list[BulkBuyItem]
    weight_oz: float
    length_in: float
    width_in:  float
    height_in: float


@router.post("/shipping/bulk-buy", dependencies=[Depends(require_write_access)])
async def bulk_buy_shipping_labels(
    body: BulkBuyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Buys one label per item, sequentially (not concurrent — these are real
    purchases; isolating errors and staying under Shippo's rate limits
    matters more than speed here). Local orders are validated as eligible
    BEFORE any purchase happens, so an ineligible order never burns a real
    label purchase. Shopify orders get marked fulfilled immediately after —
    if that specific step fails, the label itself is still valid and paid
    for, so it's reported as a warning on that item, not a failure.
    """
    from services.shippo import ShippoClient, ShippoError
    from services.shopify import create_fulfillment, ShopifyOrderError
    client = ShippoClient()

    results = []
    for item in body.items:
        ref = item.ref
        order = None

        if not ref.startswith("shopify:"):
            result = await db.execute(select(Order).where(Order.id == ref))
            order = result.scalar_one_or_none()
            if not order:
                results.append({"ref": ref, "success": False, "error": "Order not found"})
                continue
            if not _shippo_order_eligible(order):
                results.append({"ref": ref, "success": False, "error": "Order is not eligible for Shippo (must be paid, CAD, Interac)"})
                continue
            if order.tracking_number:
                results.append({"ref": ref, "success": False, "error": "Order already has a label"})
                continue

        try:
            label = await client.buy_label(rate_id=item.rate_id, order_id=ref, carrier=item.carrier)
        except ShippoError as e:
            results.append({"ref": ref, "success": False, "error": str(e)})
            continue

        if ref.startswith("shopify:"):
            _, store, shopify_id = ref.split(":", 2)
            fulfill_error = None
            try:
                await create_fulfillment(
                    int(shopify_id), store=store,
                    tracking_number=label["tracking_number"],
                    tracking_url=label["tracking_url"],
                    carrier=label["carrier"],
                )
            except ShopifyOrderError as e:
                fulfill_error = str(e)
            entry = {"ref": ref, "success": True, **label}
            if fulfill_error:
                entry["fulfillError"] = fulfill_error
            results.append(entry)
            await log_admin_activity(
                db, request,
                action="create_label", target_type="shopify_order", target_id=ref,
                details=f"{label['carrier']} — {label['tracking_number']} (bulk)"[:200],
            )
        else:
            order.tracking_number       = label["tracking_number"]
            order.tracking_url          = label["tracking_url"]
            order.carrier                = label["carrier"]
            order.label_url              = label["label_url"]
            order.shippo_transaction_id  = label["transaction_id"]
            order.shipped_at             = datetime.now(timezone.utc)
            order.package_weight_oz      = body.weight_oz
            order.package_length_in      = body.length_in
            order.package_width_in       = body.width_in
            order.package_height_in      = body.height_in
            order.paid_via_shippo        = True
            await db.commit()
            await log_admin_activity(
                db, request,
                action="create_label", target_type="order", target_id=ref,
                details=f"{label['carrier']} — {label['tracking_number']} (bulk)"[:200],
            )
            results.append({"ref": ref, "success": True, **label})

    return {"success": True, "results": results}


# ─── Email preview ────────────────────────────────────────────────────────────

@router.get("/orders/{order_id}/email-preview")
async def email_preview(
    order_id: str,
    received_amount: float = Query(0, ge=0),  # 0 = standard reminder; > 0 = partial
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_method not in (PaymentMethod.interac, PaymentMethod.zelle):
        raise HTTPException(400, "Email templates only apply to Interac/Zelle orders")

    payment_email, accent = await _resolve_payment_email(order, db)

    from services.email import build_payment_reminder_template
    tpl = build_payment_reminder_template(order, received_amount, payment_email, accent)
    return tpl


# ─── List emails sent for an order ────────────────────────────────────────────

@router.get("/orders/{order_id}/emails")
async def list_order_emails(order_id: str, db: AsyncSession = Depends(get_db)):
    """Returns all customer emails sent for this order, newest first."""
    from models.order import CustomerEmailLog
    result = await db.execute(
        select(CustomerEmailLog)
        .where(CustomerEmailLog.order_id == order_id)
        .order_by(desc(CustomerEmailLog.sent_at))
    )
    logs = result.scalars().all()

    return [
        {
            "id":        log.id,
            "type":      log.email_type,
            "sentTo":    log.sent_to,
            "subject":   log.subject,
            "bodyHtml":  log.body_html,
            "bodyText":  log.body_text,
            "sentBy":    log.sent_by,
            "success":   bool(log.success),
            "sentAt":    log.sent_at.isoformat() if log.sent_at else None,
        }
        for log in logs
    ]


# ─── All emails sent, across every order — dedicated Emails tab ──────────────

@router.get("/emails")
async def list_all_emails(
    email_type: Optional[str] = Query(None),   # "confirmation" | "reminder" | "underpaid"
    success:    Optional[str] = Query(None),    # "yes" | "no"
    q:          Optional[str] = Query(None),    # matches order ID or recipient email
    limit:      int           = Query(100, le=5000),
    offset:     int           = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Every customer-facing email ever sent — admin reminders/underpaid
    notices AND automatic post-payment confirmations (sent_by="system") —
    in one searchable, filterable list. Newest first."""
    from models.order import CustomerEmailLog

    query = select(CustomerEmailLog).order_by(desc(CustomerEmailLog.sent_at))
    if email_type:
        query = query.where(CustomerEmailLog.email_type == email_type)
    if success == "yes":
        query = query.where(CustomerEmailLog.success == 1)
    elif success == "no":
        query = query.where(CustomerEmailLog.success == 0)
    if q:
        like = f"%{q}%"
        query = query.where(_sa_or(
            CustomerEmailLog.order_id.ilike(like),
            CustomerEmailLog.sent_to.ilike(like),
        ))

    result = await db.execute(query.limit(limit).offset(offset))
    logs = result.scalars().all()

    return [
        {
            "id":        log.id,
            "orderId":   log.order_id,
            "type":      log.email_type,
            "sentTo":    log.sent_to,
            "subject":   log.subject,
            "bodyHtml":  log.body_html,
            "bodyText":  log.body_text,
            "sentBy":    log.sent_by,
            "success":   bool(log.success),
            "sentAt":    log.sent_at.isoformat() if log.sent_at else None,
        }
        for log in logs
    ]


# ─── Send payment reminder (unified $0 / partial flow) ────────────────────────

@router.post("/orders/{order_id}/send-reminder", dependencies=[Depends(require_write_access)])
async def send_payment_reminder(
    order_id: str,
    body: SendReminderRequest,   # now expects {received_amount, ...}
    db: AsyncSession = Depends(get_db),
):
    """
    Single reminder endpoint covering both scenarios:
      - received_amount == 0 → standard pending nudge, no DB flag change
      - received_amount  > 0 → flag interac/zelle as underpaid + send email
    """
    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.interac_payment))
        .options(selectinload(Order.zelle_payment))
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status == PaymentStatus.paid:
        raise HTTPException(400, "Order is already paid — no reminder needed")
    if order.payment_method not in (PaymentMethod.interac, PaymentMethod.zelle):
        raise HTTPException(400, "Reminders only apply to Interac/Zelle orders")

    received = float(body.received_amount or 0)
    total    = float(order.total)

    if received < 0:
        raise HTTPException(400, "received_amount must be 0 or greater")
    if received >= total:
        raise HTTPException(400, "received_amount is not less than total — use mark-paid instead")

    payment_email, accent = await _resolve_payment_email(order, db)

    # Build and send email FIRST — only commit DB changes if delivery succeeded
    from services.email import build_payment_reminder_template, send_email
    from models.order import CustomerEmailLog

    tpl = build_payment_reminder_template(order, received, payment_email, accent)
    tpl = _apply_overrides(tpl, body)

    sent = await send_email(
        to=order.email,
        subject=tpl["subject"],
        html=tpl["html"],
        text=tpl.get("text"),
    )

    # Always log the attempt (success OR failure) — useful for debugging
    db.add(CustomerEmailLog(
        order_id   = order_id,
        email_type = "underpaid" if received > 0 else "reminder",
        sent_to    = order.email,
        subject    = tpl["subject"],
        body_text  = tpl.get("text"),
        body_html  = tpl["html"],
        sent_by    = "admin",
        success    = 1 if sent else 0,
    ))

    if not sent:
        # Email delivery failed — DO NOT flag underpaid or update timestamps.
        # Return a 502 so the dashboard shows a clear error toast.
        await db.commit()  # commit the failed-attempt log
        raise HTTPException(
            502,
            "Email delivery failed — check email service quota or recipient address. "
            "Order was NOT flagged as underpaid; you can retry."
        )

    # Email succeeded — flag underpaid + update tracking
    if order.payment_method == PaymentMethod.interac:
        if not order.interac_payment:
            raise HTTPException(400, "No InteracPayment record on this order")
        order.interac_payment.received_amount = received
        order.interac_payment.status          = "underpaid"
    else:
        if not order.zelle_payment:
            raise HTTPException(400, "No ZellePayment record on this order")
        order.zelle_payment.received_amount = received
        order.zelle_payment.status          = "underpaid"

    order.payment_notes = body.notes or (
        f"Underpaid: received ${received:.2f} of ${total:.2f}"
        if received > 0
        else f"Reminded — full ${total:.2f} outstanding"
    )

    order.last_customer_email_at = datetime.now(timezone.utc)
    order.customer_emails_sent   = (order.customer_emails_sent or 0) + 1

    await db.commit()

    return {
        "success":      True,
        "orderId":      order_id,
        "underpaidSet": True,
        "remaining":    round(total - received, 2),
    }


# ─── Legacy alias: keep mark-underpaid working for any pre-existing callers ──

@router.post("/orders/{order_id}/mark-underpaid", dependencies=[Depends(require_write_access)])
async def mark_order_underpaid(
    order_id: str,
    body: MarkUnderpaidRequest,
    db: AsyncSession = Depends(get_db),
):
    """Deprecated. Forwards to unified send-reminder endpoint."""
    forward = SendReminderRequest(
        received_amount=body.received_amount,
        notes=body.notes,
        custom_subject=body.custom_subject,
        custom_html=body.custom_html,
        custom_text=body.custom_text,
    )
    return await send_payment_reminder(order_id, forward, db)


@router.post("/orders/{order_id}/cancel", dependencies=[Depends(require_write_access)])
async def cancel_order(
    order_id: str,
    body: MarkPaidRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.interac_payment))
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status != PaymentStatus.pending:
        raise HTTPException(400, "Only pending orders can be cancelled")

    order.payment_status = PaymentStatus.cancelled
    order.payment_notes = body.notes or "Cancelled by admin"

    await db.commit()
    await log_admin_activity(
        db, request,
        action="cancel", target_type="order", target_id=order_id,
        details=(body.notes or "Cancelled by admin")[:200],
    )
    return {"success": True, "orderId": order_id}


# ─── Order recovery ───────────────────────────────────────────────────────────

class RecoverRequest(BaseModel):
    notes:      Optional[str] = None
    send_email: bool          = False     # set true once recovery email template exists


@router.post("/orders/{order_id}/recover", dependencies=[Depends(require_write_access)])
async def recover_order(
    order_id: str,
    body: RecoverRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Reset a failed/expired order back to pending so the customer can retry payment.
    Clears stale per-method invoice records (BTCPay/NowPayments) which would have
    expired alongside the order.
    """
    from models.order import CryptoInvoice, NowPaymentsInvoice

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.crypto_invoice))
        .options(selectinload(Order.nowpayments_invoice))
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status == PaymentStatus.paid:
        raise HTTPException(400, "Cannot recover a paid order")
    if order.payment_status not in (PaymentStatus.failed, PaymentStatus.expired):
        raise HTTPException(
            400,
            f"Order is {order.payment_status.value} — only failed/expired orders can be recovered",
        )

    prev_status = order.payment_status.value

    order.payment_status = PaymentStatus.pending
    order.payment_notes  = body.notes or f"Recovered from {prev_status} status by admin"

    # Wipe stale crypto/altcoin invoice rows — they're tied to the dead session
    if order.crypto_invoice:
        await db.execute(
            CryptoInvoice.__table__.delete().where(CryptoInvoice.order_id == order.id)
        )
    if order.nowpayments_invoice:
        await db.execute(
            NowPaymentsInvoice.__table__.delete().where(NowPaymentsInvoice.order_id == order.id)
        )

    await db.commit()

    # Hook for recovery email — wire up once the template exists
    if body.send_email and order.email:
        try:
            from services.email import send_recovery_email   # implement when ready
            await send_recovery_email(order)
        except ImportError:
            logger.info(f"Recovery email skipped for {order_id} — template not implemented yet")
        except Exception as e:
            logger.error(f"Recovery email failed for {order_id}: {e}")

    logger.info(f"✅ Order {order_id} recovered ({prev_status} → pending)")
    await log_admin_activity(
        db, request,
        action="recover", target_type="order", target_id=order_id,
        details=f"{prev_status} → pending",
    )
    return {"success": True, "orderId": order_id, "previousStatus": prev_status}


# ─── Unmark paid (revert accidentally-paid orders) ────────────────────────────

class UnmarkPaidRequest(BaseModel):
    notes:       Optional[str] = None
    new_status:  str           = "pending"   # "pending" | "cancelled" | "failed"


@router.post("/orders/{order_id}/unmark-paid", dependencies=[Depends(require_write_access)])
async def unmark_order_paid(
    order_id: str,
    body: UnmarkPaidRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Reverse an accidental mark-paid. Flips a paid order back to pending (default),
    cancelled, or failed. Clears paid_at and preserves the prior payment_notes
    in an audit trail.

    Downstream side effects this endpoint does NOT undo (admin must handle):
      - Shopify order already created → manually cancel in Shopify admin
      - Affiliate webhook already fired → may need a reversal ping
      - Customer email already sent → may need a follow-up
    The reasoning is left to the admin since each case is different.
    """
    valid_targets = {"pending", "cancelled", "failed"}
    target = (body.new_status or "pending").lower()
    if target not in valid_targets:
        raise HTTPException(
            400, f"new_status must be one of {sorted(valid_targets)}"
        )

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status != PaymentStatus.paid:
        raise HTTPException(
            400,
            f"Order is {order.payment_status.value} — only paid orders can be unmarked",
        )

    prior_notes = order.payment_notes or ""
    prior_paid_at = order.paid_at.isoformat() if order.paid_at else "unknown"
    audit_line   = (
        f"[unmark-paid @ {datetime.now(timezone.utc).isoformat()}] "
        f"reverted from paid (paid_at={prior_paid_at}) → {target}. "
        f"Reason: {body.notes or 'no reason given'}. "
        f"Prior notes: {prior_notes[:200]}"
    )

    order.payment_status = PaymentStatus(target)
    order.paid_at        = None
    order.payment_notes  = audit_line[:1000]   # cap to keep the column sane

    await db.commit()

    logger.warning(
        f"⚠️  Order {order_id} unmarked-paid by admin: paid → {target}. "
        f"Reason: {body.notes or 'no reason given'}"
    )
    await log_admin_activity(
        db, request,
        action="unmark_paid", target_type="order", target_id=order_id,
        details=f"paid → {target}. Reason: {(body.notes or 'no reason given')[:200]}",
    )
    return {
        "success":        True,
        "orderId":        order_id,
        "newStatus":      target,
        "priorPaidAt":    prior_paid_at,
        "warning":        "Shopify/affiliate side effects are NOT auto-reversed.",
    }


# ─── Interac manual matching ──────────────────────────────────────────────────

@router.get("/interac/unmatched")
async def list_unmatched_interac(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(InteracPayment)
        .where(InteracPayment.status == "unmatched")
        .order_by(desc(InteracPayment.created_at))
    )
    payments = result.scalars().all()

    return [
        {
            "id":             p.id,
            "orderId":        p.order_id,
            "expectedAmount": float(p.expected_amount),
            "senderEmail":    p.sender_email,
            "notes":          p.notes,
            "createdAt":      p.created_at.isoformat(),
        }
        for p in payments
    ]


class ManualMatchRequest(BaseModel):
    interac_payment_id: int
    order_id: str


@router.post("/interac/match", dependencies=[Depends(require_write_access)])
async def manual_interac_match(
    body: ManualMatchRequest,
    db: AsyncSession = Depends(get_db),
):
    # Fetch interac record
    ip_result = await db.execute(
        select(InteracPayment).where(InteracPayment.id == body.interac_payment_id)
    )
    ip = ip_result.scalar_one_or_none()
    if not ip:
        raise HTTPException(404, "InteracPayment record not found")

    # Fetch order with items eagerly loaded (needed for create_shopify_order)
    ord_result = await db.execute(
        select(Order).where(Order.id == body.order_id)
        .options(selectinload(Order.items))
    )
    order = ord_result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    # Update both
    ip.order_id   = body.order_id
    ip.status     = "manual"
    ip.matched_at = datetime.now(timezone.utc)

    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.now(timezone.utc)
    order.payment_notes  = f"Manually matched to Interac payment #{ip.id}"

    await db.commit()

    ord_result = await db.execute(
        select(Order).where(Order.id == body.order_id)
        .options(selectinload(Order.items))
    )
    order = ord_result.scalar_one_or_none()

    from services.order_finalize import finalize_paid_order
    result_info = await finalize_paid_order(order, db, label="interac-manual-match")

    resp = {"success": True, "orderId": order.id}
    if result_info.get("shopify_error"):
        resp["shopifyError"] = result_info["shopify_error"]
    if result_info.get("affiliate_error"):
        resp["affiliateError"] = result_info["affiliate_error"]
    return resp


# ─── Zelle manual matching ────────────────────────────────────────────────────

@router.get("/zelle/unmatched")
async def list_unmatched_zelle(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ZellePayment)
        .where(ZellePayment.status == "unmatched")
        .order_by(desc(ZellePayment.created_at))
    )
    payments = result.scalars().all()

    return [
        {
            "id":             p.id,
            "orderId":        p.order_id,
            "expectedAmount": float(p.expected_amount),
            "senderEmail":    p.sender_email,
            "notes":          p.notes,
            "createdAt":      p.created_at.isoformat(),
        }
        for p in payments
    ]


class ManualZelleMatchRequest(BaseModel):
    zelle_payment_id: int
    order_id: str


@router.post("/zelle/match", dependencies=[Depends(require_write_access)])
async def manual_zelle_match(
    body: ManualZelleMatchRequest,
    db: AsyncSession = Depends(get_db),
):
    zp_result = await db.execute(
        select(ZellePayment).where(ZellePayment.id == body.zelle_payment_id)
    )
    zp = zp_result.scalar_one_or_none()
    if not zp:
        raise HTTPException(404, "ZellePayment record not found")

    ord_result = await db.execute(
        select(Order).where(Order.id == body.order_id)
        .options(selectinload(Order.items))
    )
    order = ord_result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    zp.order_id   = body.order_id
    zp.status     = "manual"
    zp.matched_at = datetime.now(timezone.utc)

    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.now(timezone.utc)
    order.payment_notes  = f"Manually matched to Zelle payment #{zp.id}"

    await db.commit()

    ord_result = await db.execute(
        select(Order).where(Order.id == body.order_id)
        .options(selectinload(Order.items))
    )
    order = ord_result.scalar_one_or_none()

    from services.order_finalize import finalize_paid_order
    result_info = await finalize_paid_order(order, db, label="zelle-manual-match")

    resp = {"success": True, "orderId": order.id}
    if result_info.get("shopify_error"):
        resp["shopifyError"] = result_info["shopify_error"]
    if result_info.get("affiliate_error"):
        resp["affiliateError"] = result_info["affiliate_error"]
    return resp


# ─── Brands ──────────────────────────────────────────────────────────────────

# ─── Monitoring / system-health dashboard ─────────────────────────────────────

@router.get("/monitoring/health")
async def monitoring_health(db: AsyncSession = Depends(get_db)):
    """
    Aggregate health snapshot for the Dashboard tab in the admin UI.
    Returns: server, processors, today_kpis, sources, recent_events.
    Designed to be polled every 30s.

    Cached in Redis for 8s (see _cache_get/_cache_set) — this runs ~20 small
    queries (7 processors x 2 activity/volume queries, plus KPIs/sources/
    recent-events), so caching lets concurrent admins share one computed
    result instead of each re-running the full set.
    """
    cache_key = "admin:monitoring:health"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    from sqlalchemy import func
    today_start = _today_start_utc()

    # ── server ────────────────────────────────────────────────────────────────
    db_ok = True
    try:
        from sqlalchemy import text as _sqltext
        await db.execute(_sqltext("SELECT 1"))
    except Exception:
        db_ok = False

    server = {
        "env":   settings.ENVIRONMENT,
        "db_ok": db_ok,
        "base_url": settings.BASE_URL,
    }

    # ── processors (configured + most-recent activity) ────────────────────────
    # "Last activity" = paid_at of the most recent paid order using that method
    # (falls back to created_at when nothing paid yet).
    async def _last_activity(method: PaymentMethod, ref_filter=None) -> dict:
        q = select(Order.paid_at, Order.created_at, Order.total, Order.currency).where(
            Order.payment_method == method
        )
        if ref_filter is not None:
            q = q.where(ref_filter)
        q = q.order_by(desc(Order.created_at)).limit(1)
        row = (await db.execute(q)).first()
        return {
            "last_paid":    row[0].isoformat() if row and row[0] else None,
            "last_created": row[1].isoformat() if row and row[1] else None,
        }

    async def _today_volume(method: PaymentMethod, ref_filter=None) -> dict:
        q = (
            select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
            .where(Order.payment_method == method)
            .where(Order.payment_status == PaymentStatus.paid)
            .where(Order.paid_at >= today_start)
        )
        if ref_filter is not None:
            q = q.where(ref_filter)
        cnt, rev = (await db.execute(q)).first()
        return {"paid_count": int(cnt or 0), "paid_revenue": float(rev or 0)}

    processors = {}

    # pymtz card is blanket-disabled for EVERY store right now — see
    # `if country in ("US", "CA"): card_enabled = False` in main.py's
    # checkout_page(). Since every order is always US or CA, this is a
    # global kill switch, not a per-store one. "configured" still reflects
    # whether credentials exist (so you can tell "off on purpose" apart from
    # "never set up"), but "enabled" must also reflect this override —
    # otherwise this health check just checks credential presence and
    # disagrees with what's actually offered at checkout. If that branch in
    # main.py is ever removed to re-enable pymtz, update PYMTZ_CARD_LIVE too.
    PYMTZ_CARD_LIVE = False

    # pymtz — same payment_method ("card") for both CA and US; disambiguate
    # by metadata. payment_ref starts with "pay_" → pymtz; we can split by
    # the order.currency to attribute CA vs US.
    pymtz_ca_act = await _last_activity(PaymentMethod.card, Order.currency == "CAD")
    pymtz_ca_vol = await _today_volume(PaymentMethod.card, Order.currency == "CAD")
    processors["pymtz_ca"] = {
        "label":      "pymtz CA",
        "configured": bool(settings.PYMTZ_API_KEY_CA or settings.PYMTZ_API_KEY),
        "enabled":    PYMTZ_CARD_LIVE and bool(settings.PYMTZ_API_KEY_CA or settings.PYMTZ_API_KEY),
        "mode":       "LIVE" if (settings.PYMTZ_API_KEY_CA or "").startswith("pymtz_live_") else "TEST",
        **pymtz_ca_act, **pymtz_ca_vol,
    }

    pymtz_us_act = await _last_activity(PaymentMethod.card, Order.currency == "USD")
    pymtz_us_vol = await _today_volume(PaymentMethod.card, Order.currency == "USD")
    processors["pymtz_us"] = {
        "label":      "pymtz US",
        "configured": bool(settings.PYMTZ_API_KEY_US or settings.PYMTZ_API_KEY),
        "enabled":    PYMTZ_CARD_LIVE and bool(settings.PYMTZ_API_KEY_US or settings.PYMTZ_API_KEY),
        "mode":       "LIVE" if (settings.PYMTZ_API_KEY_US or "").startswith("pymtz_live_") else "TEST",
        **pymtz_us_act, **pymtz_us_vol,
    }

    # BTCPay (crypto)
    btcpay_configured = bool(settings.BTCPAY_API_KEY and settings.BTCPAY_STORE_ID)
    btcpay_act = await _last_activity(PaymentMethod.crypto)
    btcpay_vol = await _today_volume(PaymentMethod.crypto)
    processors["btcpay"] = {
        "label":      "BTCPay (crypto)",
        "configured": btcpay_configured,
        "enabled":    btcpay_configured,
        "mode":       "LIVE",
        **btcpay_act, **btcpay_vol,
    }

    # NowPayments (altcoin) — "configured" is just "is the API key set";
    # "enabled" also needs the ALTCOIN_ENABLED master kill-switch (same
    # condition main.py uses to decide whether the altcoin option actually
    # shows at checkout), or this reports LIVE while it's deliberately
    # switched off.
    nowp_configured = bool(settings.NOWPAYMENTS_API_KEY)
    nowp_enabled = nowp_configured and bool(getattr(settings, "ALTCOIN_ENABLED", True))
    nowp_act = await _last_activity(PaymentMethod.altcoin)
    nowp_vol = await _today_volume(PaymentMethod.altcoin)
    processors["nowpayments"] = {
        "label":      "NowPayments",
        "configured": nowp_configured,
        "enabled":    nowp_enabled,
        "mode":       "LIVE",
        **nowp_act, **nowp_vol,
    }

    # Interac
    interac_act = await _last_activity(PaymentMethod.interac)
    interac_vol = await _today_volume(PaymentMethod.interac)
    processors["interac"] = {
        "label":      "Interac",
        "configured": bool(settings.INTERAC_DEFAULT_EMAIL),
        "enabled":    bool(settings.INTERAC_DEFAULT_EMAIL),
        "mode":       "LIVE",
        **interac_act, **interac_vol,
    }

    # Zelle
    zelle_act = await _last_activity(PaymentMethod.zelle)
    zelle_vol = await _today_volume(PaymentMethod.zelle)
    processors["zelle"] = {
        "label":      "Zelle",
        "configured": bool(settings.ZELLE_DEFAULT_EMAIL),
        "enabled":    bool(settings.ZELLE_DEFAULT_EMAIL),
        "mode":       "LIVE",
        **zelle_act, **zelle_vol,
    }

    # Onramp WP (the experimental rail)
    processors["onramp_wp"] = {
        "label":      "Onramp (WP)",
        "configured": bool(getattr(settings, "ONRAMP_WP_URL", "")),
        "enabled":    bool(getattr(settings, "ONRAMP_WP_ENABLED", False)),
        "mode":       "LIVE",
        "last_paid":  None,
        "last_created": None,
        "paid_count": 0,
        "paid_revenue": 0.0,
    }

    # Shopify — two separate stores (CA/US), same split as pymtz. Unlike
    # every processor above, "enabled" here also requires a LIVE, cached key
    # check (see _shopify_live_status) — a bad/expired API key is exactly
    # the failure mode this card exists to catch, not just presence.
    # keyInvalid distinguishes "confirmed broken" (red dot) from "not set up"
    # (gray dot) on the frontend — both would otherwise look identical.
    shopify_ca_domain = getattr(settings, "SHOPIFY_STORE_DOMAIN", "") or ""
    shopify_ca_token  = getattr(settings, "SHOPIFY_API_TOKEN", "") or ""
    shopify_ca_configured = bool(shopify_ca_domain and shopify_ca_token)
    shopify_ca_live = await _shopify_live_status(shopify_ca_domain, shopify_ca_token, "ca")
    processors["shopify_ca"] = {
        "label":        "Shopify CA",
        "configured":   shopify_ca_configured,
        "enabled":      shopify_ca_configured and shopify_ca_live is not False,
        "keyInvalid":   shopify_ca_live is False,
        "mode":         "LIVE",
        "last_paid":    None, "last_created": None,
        "paid_count":   0,    "paid_revenue": 0.0,
    }

    shopify_us_domain = getattr(settings, "SHOPIFY_STORE_DOMAIN_US", "") or ""
    shopify_us_token  = getattr(settings, "SHOPIFY_API_TOKEN_US", "") or ""
    shopify_us_configured = bool(shopify_us_domain and shopify_us_token)
    shopify_us_live = await _shopify_live_status(shopify_us_domain, shopify_us_token, "us")
    processors["shopify_us"] = {
        "label":        "Shopify US",
        "configured":   shopify_us_configured,
        "enabled":      shopify_us_configured and shopify_us_live is not False,
        "keyInvalid":   shopify_us_live is False,
        "mode":         "LIVE",
        "last_paid":    None, "last_created": None,
        "paid_count":   0,    "paid_revenue": 0.0,
    }

    # Shippo — shipping labels. Activity here is real, DB-backed data
    # (Order.shipped_at, added with the Shippo integration) rather than the
    # zeroed-out placeholders above, since we actually track label purchases.
    shippo_token = getattr(settings, "SHIPPO_API_TOKEN", "") or ""
    shippo_master_enabled = bool(getattr(settings, "SHIPPO_ENABLED", False))
    shippo_live = await _shippo_live_status(shippo_token)
    shippo_last_shipped = (await db.execute(
        select(func.max(Order.shipped_at)).where(Order.shipped_at.isnot(None))
    )).scalar()
    shippo_today_count = (await db.execute(
        select(func.count(Order.id)).where(Order.shipped_at >= today_start)
    )).scalar()
    processors["shippo"] = {
        "label":        "Shippo",
        "configured":   bool(shippo_token),
        "enabled":      bool(shippo_token) and shippo_master_enabled and shippo_live is not False,
        "keyInvalid":   shippo_live is False,
        "mode":         "LIVE",
        "last_paid":    shippo_last_shipped.isoformat() if shippo_last_shipped else None,
        "last_created": None,
        "paid_count":   int(shippo_today_count or 0),
        "paid_revenue": 0.0,
    }

    # Affiliate webhook — no safe read-only ping exists for this (it's a
    # one-way webhook receiver, not a queryable API), so this reflects
    # configuration only, not confirmed live validity like the two above.
    processors["affiliate"] = {
        "label":      "Affiliate Webhook",
        "configured": bool(getattr(settings, "AFFILIATE_DASHBOARD_URL", "")),
        "enabled":    bool(getattr(settings, "AFFILIATE_DASHBOARD_URL", "")),
        "mode":       "LIVE",
        "last_paid": None, "last_created": None, "paid_count": 0, "paid_revenue": 0.0,
    }

    # ── today's KPIs ──────────────────────────────────────────────────────────
    # Two separate queries — "orders today" (created_at) is different from
    # "paid today" (paid_at). A pending order from yesterday marked paid
    # today should show up in paid_count/revenue even though it wasn't
    # created today.

    # 1. Orders created today, grouped by status — for orders_total + status breakdown
    created_today_q = (
        select(Order.payment_status, func.count(Order.id))
        .where(Order.created_at >= today_start)
        .group_by(Order.payment_status)
    )
    created_rows = (await db.execute(created_today_q)).all()
    created_by_status = {r[0].value: int(r[1]) for r in created_rows}
    total_today = sum(created_by_status.values())

    # 2. Orders PAID today (regardless of when created) — true revenue today.
    # Revenue grouped by currency, NOT summed across currencies — see the
    # identical note in order_stats() above (SUM(Order.total) across mixed
    # CAD/USD rows produces a number that isn't a real amount in either).
    # Uses the SETTLED amount/currency when present — see order_stats() for
    # why (WPay/pymtz convert CAD carts to USD at charge time).
    _paid_amount_expr   = func.coalesce(Order.settled_amount, Order.total)
    _paid_currency_expr = func.coalesce(Order.settled_currency, Order.currency)
    paid_today_q = (
        select(_paid_currency_expr, func.count(Order.id), func.coalesce(func.sum(_paid_amount_expr), 0))
        .where(Order.paid_at >= today_start)
        .where(Order.payment_status == PaymentStatus.paid)
        .group_by(_paid_currency_expr)
    )
    paid_today_rows = (await db.execute(paid_today_q)).all()
    paid_count_today = sum(int(cnt or 0) for _, cnt, _ in paid_today_rows)
    paid_revenue_today_by_currency = {
        (cur or "UNKNOWN"): float(amt or 0) for cur, _, amt in paid_today_rows
    }
    paid_revenue_today = sum(paid_revenue_today_by_currency.values())

    # 3. Currently pending (queue size, no date filter) — must match the same
    # filter the /admin/orders/stats endpoint uses for the top "Pending" stat
    # card, otherwise the dashboard KPI disagrees with the header card.
    # Excludes: dead non-pymtz card orders (never produced a payment intent)
    # and orders that already had a reminder email sent (tracked elsewhere).
    from sqlalchemy import or_
    pending_now_q = (
        select(func.count(Order.id))
        .where(Order.payment_status == PaymentStatus.pending)
        .where(or_(
            Order.payment_method != PaymentMethod.card,
            _is_delayed_card(),    # pymtz / Highriskify / WP onramp pending cards count
        ))
        .where(or_(
            Order.customer_emails_sent == 0,
            Order.customer_emails_sent.is_(None),
        ))
    )
    pending_now = int((await db.execute(pending_now_q)).scalar() or 0)

    today_kpis = {
        "orders_total":    total_today,
        "paid_count":      paid_count_today,
        "pending_count":   pending_now,
        "failed_count":    created_by_status.get("failed", 0),
        "refunded_count":  created_by_status.get("refunded", 0),
        "revenue":         round(paid_revenue_today, 2),
        "revenueByCurrency": {k: round(v, 2) for k, v in paid_revenue_today_by_currency.items()},
        # Conversion: of orders that came in today, what % paid (today OR later).
        # Approx since paid_today may include older orders. Best-effort metric.
        "conversion_rate": round((paid_count_today / total_today * 100), 1) if total_today > 0 else 0.0,
    }

    # ── sources (top 10 by PAID orders today, ranked by revenue) ─────────────
    # Filter on paid_at so an order created yesterday but paid today still
    # counts. Only include paid orders — pending/failed clutter the table.
    # Ranked by the SETTLED amount when present — see order_stats() for why.
    _src_amount_expr = func.coalesce(Order.settled_amount, Order.total)
    src_q = (
        select(Order.source_domain, func.count(Order.id), func.coalesce(func.sum(_src_amount_expr), 0))
        .where(Order.paid_at >= today_start)
        .where(Order.payment_status == PaymentStatus.paid)
        .group_by(Order.source_domain)
        .order_by(desc(func.coalesce(func.sum(_src_amount_expr), 0)))
        .limit(10)
    )
    src_rows = (await db.execute(src_q)).all()
    sources = [
        {
            "domain":  (r[0] or "(unknown)").replace("www.", ""),
            "orders":  int(r[1]),
            "revenue": float(r[2]),
        }
        for r in src_rows
    ]

    # ── recent events (last 50) ───────────────────────────────────────────────
    # Selects settled_amount/settled_currency too — see order_stats() for why
    # the settled figures (not Order.total/Order.currency) are the correct
    # "what was actually charged" numbers for WPay/pymtz orders.
    recent_q = (
        select(
            Order.id, Order.payment_status, Order.payment_method,
            Order.total, Order.currency, Order.source_domain,
            Order.created_at, Order.paid_at, Order.payment_notes,
            Order.settled_amount, Order.settled_currency,
        )
        .order_by(desc(Order.created_at))
        .limit(50)
    )
    recent_rows = (await db.execute(recent_q)).all()
    recent_events = [
        {
            "order_id":   r[0],
            "status":     r[1].value if r[1] else "unknown",
            "method":     r[2].value if r[2] else "unknown",
            "amount":     float(r[9]) if r[9] is not None else float(r[3] or 0),
            "currency":   r[10] or r[4] or "CAD",
            "source":     (r[5] or "").replace("www.", ""),
            "created_at": r[6].isoformat() if r[6] else None,
            "paid_at":    r[7].isoformat() if r[7] else None,
            "notes":      (r[8] or "")[:140],
        }
        for r in recent_rows
    ]

    result = {
        "server":         server,
        "processors":     processors,
        "today_kpis":     today_kpis,
        "sources":        sources,
        "recent_events":  recent_events,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set(cache_key, result, ttl=8)
    return result


@router.get("/monitoring/activities")
async def list_admin_activities(
    limit: int = Query(100, ge=1, le=500),
    action: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent admin actions for the Dashboard tab's activity feed."""
    q = select(AdminActivity).order_by(desc(AdminActivity.created_at)).limit(limit)
    if action:
        q = q.where(AdminActivity.action == action)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id":          r.id,
            "createdAt":   r.created_at.isoformat() if r.created_at else None,
            "adminUser":   r.admin_user,
            "action":      r.action,
            "targetType":  r.target_type,
            "targetId":    r.target_id,
            "details":     r.details,
            "ipAddress":   r.ip_address,
        }
        for r in rows
    ]


class LogActivityRequest(BaseModel):
    action:      str
    target_type: Optional[str] = None
    target_id:   Optional[str] = None
    details:     Optional[str] = None


@router.post("/monitoring/log")
async def post_admin_activity(
    body: LogActivityRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Client-side audit logger — used for things like CSV exports that
    don't go through any other admin endpoint. Same authn as everything
    else under /admin."""
    # Whitelist actions the client is allowed to log so this can't be
    # abused to spam fake audit rows.
    ALLOWED = {"export_csv", "view_email_history", "switch_email_mode"}
    if body.action not in ALLOWED:
        raise HTTPException(400, f"action '{body.action}' is not allowed via client logger")
    await log_admin_activity(
        db, request,
        action=body.action, target_type=body.target_type or "",
        target_id=body.target_id or "", details=body.details or "",
    )
    return {"success": True}


@router.get("/brands")
async def list_brands(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Brand).order_by(Brand.id))
    brands = result.scalars().all()
    return [
        {
            "id":              b.id,
            "domain":          b.domain,
            "storeName":       b.store_name,
            "interacEmail":    b.interac_email,
            "interacDiscount": float(b.interac_discount or 5),
            "cryptoDiscount":  float(b.crypto_discount or 10),
            "active":          b.active,
        }
        for b in brands
    ]


class BrandCreate(BaseModel):
    domain:           str
    store_name:       str
    logo_url:         Optional[str] = None
    header_bg_url:    Optional[str] = None
    accent_color:     str = "#dd1d1d"
    accent_hover:     str = "#b01515"
    interac_email:    Optional[str] = None
    interac_discount: float = 5.0
    crypto_discount:  float = 10.0
    helcim_api_key:   Optional[str] = None
    btcpay_store_id:  Optional[str] = None
    active:           bool = True


@router.post("/brands", status_code=201, dependencies=[Depends(require_write_access)])
async def create_brand(body: BrandCreate, db: AsyncSession = Depends(get_db)):
    brand = Brand(**body.model_dump())
    db.add(brand)
    await db.commit()
    await db.refresh(brand)
    return {"id": brand.id, "domain": brand.domain}


@router.put("/brands/{brand_id}", dependencies=[Depends(require_write_access)])
async def update_brand(
    brand_id: int,
    body: BrandCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Brand).where(Brand.id == brand_id))
    brand  = result.scalar_one_or_none()
    if not brand:
        raise HTTPException(404, "Brand not found")

    for key, val in body.model_dump().items():
        setattr(brand, key, val)

    await db.commit()
    return {"success": True}