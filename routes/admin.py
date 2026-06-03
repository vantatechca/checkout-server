"""
Internal admin endpoints (should be behind IP whitelist in Nginx, not public).

GET  /admin/orders              → list orders with filters
GET  /admin/orders/{id}         → order detail
POST /admin/orders/{id}/mark-paid   → manually mark any order paid
POST /admin/interac/match       → manually match an unmatched Interac payment
GET  /admin/interac/unmatched   → list Interac payments needing manual review
GET  /admin/brands              → list brands
POST /admin/brands              → create brand
PUT  /admin/brands/{id}         → update brand
"""
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, desc

from database import get_db
from models.order import Order, InteracPayment, ZellePayment, CryptoInvoice, NowPaymentsInvoice, PaymentStatus, PaymentMethod
from models.brand import Brand
from models.admin_activity import AdminActivity
from routes.auth_routes import require_admin
from config import settings


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
    limit:       int           = Query(50, le=2000),    # raised cap so the page can show more
    offset:      int           = Query(0),
    db: AsyncSession = Depends(get_db),
):
    q = select(Order).order_by(desc(Order.created_at))

    if status:
        q = q.where(Order.payment_status == status)
        if status == "pending":
            # Exclude card orders UNLESS they are pymtz (payment_ref starts with
            # "pay_"). Whop/Stripe/Helcim card orders are never legitimately pending;
            # pymtz orders sit pending until admin marks them paid.
            from sqlalchemy import or_ as _or
            q = q.where(
                _or(
                    Order.payment_method != PaymentMethod.card,
                    Order.payment_ref.like("pay_%"),
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
                    ((Order.payment_method != PaymentMethod.card) | Order.payment_ref.like("pay_%")) &
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

    out = []
    for o in orders:
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
            and not (
                o.payment_method == PaymentMethod.card
                and (o.payment_ref or "").startswith("pay_")
            )   # pymtz pending card orders are NOT abandoned
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

        out.append(d)
    return out


@router.get("/orders/stats")
async def order_stats(
    currency: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Server-side aggregate counts for dashboard stat cards + tab badges.
    Accurate at any scale — doesn't fetch full rows.
    """
    from sqlalchemy import func as sa_func, and_, or_
    from datetime import datetime, timezone

    base_filter = []
    if currency:
        base_filter.append(Order.currency == currency.upper())

    pending_q = select(sa_func.count()).select_from(Order).where(
        and_(
            *base_filter,
            Order.payment_status == PaymentStatus.pending,
            or_(
                Order.payment_method != PaymentMethod.card,
                Order.payment_ref.like("pay_%"),   # pymtz pending card orders
            ),
            or_(Order.customer_emails_sent == 0, Order.customer_emails_sent.is_(None)),
        )
    )

    start_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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
                Order.payment_ref.like("pay_%"),   # pymtz pending cards ARE counted
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

    # Failed = failed OR expired — both are "recoverable" terminal states
    failed_q = select(sa_func.count()).select_from(Order).where(
        and_(
            *base_filter,
            Order.payment_status.in_([PaymentStatus.failed, PaymentStatus.expired]),
        )
    )

    # Today's revenue (sum of totals)
    revenue_today_q = select(sa_func.coalesce(sa_func.sum(Order.total), 0)).where(
        and_(*base_filter, Order.payment_status == PaymentStatus.paid, Order.paid_at >= start_today)
    )

    pending_count    = (await db.execute(pending_q)).scalar_one()
    paid_count       = (await db.execute(paid_q)).scalar_one()
    paid_today_count = (await db.execute(paid_today_q)).scalar_one()
    all_count        = (await db.execute(all_q)).scalar_one()
    underpaid_count  = (await db.execute(underpaid_q)).scalar_one()
    failed_count     = (await db.execute(failed_q)).scalar_one()
    revenue_today    = float((await db.execute(revenue_today_q)).scalar_one() or 0)

    # Device breakdown across all orders (derived from stored user-agents).
    from models.order import _classify_device
    ua_q = select(Order.user_agent)
    if base_filter:
        ua_q = ua_q.where(and_(*base_filter))
    ua_rows = (await db.execute(ua_q)).scalars().all()
    device_counts = {"Mobile": 0, "Desktop": 0, "Tablet": 0, "Unknown": 0}
    for ua in ua_rows:
        device_counts[_classify_device(ua)] += 1
    device_total = sum(device_counts.values()) or 1
    device_pct = {k: round(v / device_total * 100) for k, v in device_counts.items()}

    return {
        "pending":         pending_count,
        "paid":            paid_count,
        "paidToday":       paid_today_count,
        "all":             all_count,
        "underpaid":       underpaid_count,
        "failed":          failed_count,
        "revenueToday":    revenue_today,
        "deviceCounts":    device_counts,
        "devicePct":       device_pct,
    }

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


@router.post("/orders/{order_id}/mark-paid")
async def mark_order_paid(
    order_id: str,
    body: MarkPaidRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
    select(Order).where(Order.id == order_id)
    .options(selectinload(Order.interac_payment))
    .options(selectinload(Order.zelle_payment))
    .options(selectinload(Order.items))
)
    order  = result.scalar_one_or_none()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.payment_status == PaymentStatus.paid:
        raise HTTPException(400, "Order already marked as paid")

    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.now(timezone.utc)
    order.payment_notes  = body.notes or "Manually marked paid by admin"

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

    try:
        affiliate_url = getattr(settings, "AFFILIATE_DASHBOARD_URL", "")
        if affiliate_url and order.discount_code:
            items_summary = ", ".join(
                f"{item.qty}x {item.title}" for item in (order.items or [])
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{affiliate_url}/api/webhooks/order-paid",
                    json={
                        "customer_first_name": order.first_name or order.last_name or "Customer",
                        "items_summary":       items_summary,
                        "order_total":         float(order.total),
                        "discount_code":       (order.discount_code or "").upper(),
                        "source":              "portal",
                        "external_order_id":   order.id,
                        "source_store":        order.source_domain or order.store_name or "",
                        "currency":            order.currency or "CAD",
                    },
                )
            logger.info(f"Affiliate webhook sent for {order_id}")
    except Exception as e:
        logger.warning(f"Affiliate webhook failed for {order_id}: {e}")
    shopify_order_number = None
    try:
        from services.shopify import create_shopify_order
        shopify_order = await create_shopify_order(order)
        if shopify_order:
            shopify_order_number = str(shopify_order.get("order_number", ""))
            logger.info(f"Shopify order #{shopify_order_number} created for {order.id}")
    except Exception as e:
        logger.error(f"Shopify order creation error: {e}")

    if order.email:
        try:
            brand = (await db.execute(select(Brand).where(Brand.id == order.brand_id))).scalar_one_or_none()
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            from services.email import send_confirmation_email
            await send_confirmation_email(order, shopify_order_number=shopify_order_number, accent=accent)
        except Exception as e:
            logger.error(f"Confirmation email failed for {order.id}: {e}")
    return {"success": True, "orderId": order_id}

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


# ─── List emails sent for an order ──────────────────────────────────────────

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


# ─── Send payment reminder (unified $0 / partial flow) ────────────────────────

@router.post("/orders/{order_id}/send-reminder")
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

@router.post("/orders/{order_id}/mark-underpaid")
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


@router.post("/orders/{order_id}/cancel")
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

    order.payment_status = PaymentStatus.failed
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


@router.post("/orders/{order_id}/recover")
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


@router.post("/orders/{order_id}/unmark-paid")
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


@router.post("/interac/match")
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

    shopify_order_number = None
    try:
        from services.shopify import create_shopify_order
        shopify_order = await create_shopify_order(order)
        if shopify_order:
            shopify_order_number = str(shopify_order.get("order_number", ""))
            logger.info(f"Shopify order #{shopify_order_number} created for {order.id}")
    except Exception as e:
        logger.error(f"Shopify order creation error: {e}")

    if order.email:
        try:
            brand = (await db.execute(select(Brand).where(Brand.id == order.brand_id))).scalar_one_or_none()
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            from services.email import send_confirmation_email
            await send_confirmation_email(order, shopify_order_number=shopify_order_number, accent=accent)
        except Exception as e:
            logger.error(f"Confirmation email failed for {order.id}: {e}")

    return {"success": True, "orderId": order.id}


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


@router.post("/zelle/match")
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

    shopify_order_number = None
    try:
        from services.shopify import create_shopify_order
        shopify_order = await create_shopify_order(order)
        if shopify_order:
            shopify_order_number = str(shopify_order.get("order_number", ""))
            logger.info(f"Shopify order #{shopify_order_number} created for {order.id}")
    except Exception as e:
        logger.error(f"Shopify order creation error: {e}")

    if order.email:
        try:
            brand = (await db.execute(select(Brand).where(Brand.id == order.brand_id))).scalar_one_or_none()
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            from services.email import send_confirmation_email
            await send_confirmation_email(order, shopify_order_number=shopify_order_number, accent=accent)
        except Exception as e:
            logger.error(f"Confirmation email failed for {order.id}: {e}")

    return {"success": True, "orderId": order.id}


# ─── Monitoring / system-health dashboard ─────────────────────────────────────

@router.get("/monitoring/health")
async def monitoring_health(db: AsyncSession = Depends(get_db)):
    """
    Aggregate health snapshot for the Dashboard tab in the admin UI.
    Returns: server, processors, today_kpis, sources, recent_events.
    Designed to be polled every 30s; no expensive queries.
    """
    from sqlalchemy import func
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # ── server ──────────────────────────────────────────────────────────────
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

    # pymtz — same payment_method ("card") for both CA and US; disambiguate
    # by metadata. payment_ref starts with "pay_" → pymtz; we can split by
    # the order.currency to attribute CA vs US.
    pymtz_ca_act = await _last_activity(PaymentMethod.card, Order.currency == "CAD")
    pymtz_ca_vol = await _today_volume(PaymentMethod.card, Order.currency == "CAD")
    processors["pymtz_ca"] = {
        "label":      "pymtz CA",
        "configured": bool(settings.PYMTZ_API_KEY_CA or settings.PYMTZ_API_KEY),
        "enabled":    bool(settings.PYMTZ_API_KEY_CA or settings.PYMTZ_API_KEY),
        "mode":       "LIVE" if (settings.PYMTZ_API_KEY_CA or "").startswith("pymtz_live_") else "TEST",
        **pymtz_ca_act, **pymtz_ca_vol,
    }

    pymtz_us_act = await _last_activity(PaymentMethod.card, Order.currency == "USD")
    pymtz_us_vol = await _today_volume(PaymentMethod.card, Order.currency == "USD")
    processors["pymtz_us"] = {
        "label":      "pymtz US",
        "configured": bool(settings.PYMTZ_API_KEY_US or settings.PYMTZ_API_KEY),
        "enabled":    bool(settings.PYMTZ_API_KEY_US or settings.PYMTZ_API_KEY),
        "mode":       "LIVE" if (settings.PYMTZ_API_KEY_US or "").startswith("pymtz_live_") else "TEST",
        **pymtz_us_act, **pymtz_us_vol,
    }

    # Whop
    whop_configured = bool(settings.WHOP_API_KEY or settings.WHOP_SANDBOX_API_KEY)
    processors["whop"] = {
        "label":      "Whop",
        "configured": whop_configured,
        "enabled":    bool(getattr(settings, "WHOP_ENABLED", False)),
        "mode":       "SANDBOX" if getattr(settings, "WHOP_SANDBOX", False) else "LIVE",
        "last_paid":    None,
        "last_created": None,
        "paid_count":   0,
        "paid_revenue": 0.0,
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

    # NowPayments (altcoin)
    nowp_configured = bool(settings.NOWPAYMENTS_API_KEY)
    nowp_act = await _last_activity(PaymentMethod.altcoin)
    nowp_vol = await _today_volume(PaymentMethod.altcoin)
    processors["nowpayments"] = {
        "label":      "NowPayments",
        "configured": nowp_configured,
        "enabled":    nowp_configured,
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

    # 2. Orders PAID today (regardless of when created) — true revenue today
    paid_today_q = (
        select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(Order.paid_at >= today_start)
        .where(Order.payment_status == PaymentStatus.paid)
    )
    paid_row = (await db.execute(paid_today_q)).first()
    paid_count_today   = int(paid_row[0] or 0)
    paid_revenue_today = float(paid_row[1] or 0)

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
            Order.payment_ref.like("pay_%"),   # pymtz pending card orders count
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
        # Conversion: of orders that came in today, what % paid (today OR later).
        # Approx since paid_today may include older orders. Best-effort metric.
        "conversion_rate": round((paid_count_today / total_today * 100), 1) if total_today > 0 else 0.0,
    }

    # ── sources (top 10 by PAID orders today, ranked by revenue) ──────────────
    # Filter on paid_at so an order created yesterday but paid today still
    # counts. Only include paid orders — pending/failed clutter the table.
    src_q = (
        select(Order.source_domain, func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(Order.paid_at >= today_start)
        .where(Order.payment_status == PaymentStatus.paid)
        .group_by(Order.source_domain)
        .order_by(desc(func.coalesce(func.sum(Order.total), 0)))
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
    recent_q = (
        select(
            Order.id, Order.payment_status, Order.payment_method,
            Order.total, Order.currency, Order.source_domain,
            Order.created_at, Order.paid_at, Order.payment_notes,
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
            "amount":     float(r[3] or 0),
            "currency":   r[4] or "CAD",
            "source":     (r[5] or "").replace("www.", ""),
            "created_at": r[6].isoformat() if r[6] else None,
            "paid_at":    r[7].isoformat() if r[7] else None,
            "notes":      (r[8] or "")[:140],
        }
        for r in recent_rows
    ]

    return {
        "server":         server,
        "processors":     processors,
        "today_kpis":     today_kpis,
        "sources":        sources,
        "recent_events":  recent_events,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
    }


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


@router.post("/brands", status_code=201)
async def create_brand(body: BrandCreate, db: AsyncSession = Depends(get_db)):
    brand = Brand(**body.model_dump())
    db.add(brand)
    await db.commit()
    await db.refresh(brand)
    return {"id": brand.id, "domain": brand.domain}


@router.put("/brands/{brand_id}")
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
