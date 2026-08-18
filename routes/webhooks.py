"""
POST /webhooks/btcpay         → BTCPay Server payment notifications
POST /webhooks/shopify-paid   → Shopify order paid (from bridge stores)
"""
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Request, HTTPException, Header
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models.order import Order, CryptoInvoice, NowPaymentsInvoice, PaymentMethod, PaymentStatus
from services.btcpay import verify_btcpay_webhook, BTCPAY_STATUS_MAP, BTCPayClient
from services.nowpayments import verify_nowpayments_ipn, NOWPAYMENTS_STATUS_MAP
from services.pymtz import PYMTZ_STATUS_MAP
import httpx

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


async def _send_affiliate_webhook(order) -> None:
    """
    Notify the rosicteam dashboard whenever an order transitions to paid.

    Sends for EVERY paid order — with or without a discount_code. Orders
    placed without an affiliate code still post; the `discount_code` field is
    an empty string in that case so rosicteam can record the sale under "no
    affiliate" while still tracking total order flow.

    Bails only if AFFILIATE_DASHBOARD_URL isn't configured (dev/local).
    Errors are caught + logged; no retry — failed sends are surfaced in the
    log only. Callers are expected to use the helper idempotently.
    """
    try:
        affiliate_url = getattr(settings, "AFFILIATE_DASHBOARD_URL", "")
        if not affiliate_url:
            return
        items_summary = ", ".join(
            f"{item.qty}x {item.title}" for item in (order.items or [])
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
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
        logger.info(f"Affiliate webhook sent for {order.id}: {resp.status_code}")
    except Exception as e:
        logger.warning(f"Affiliate webhook failed for {order.id}: {e}")


# ─── POST /webhooks/btcpay ────────────────────────────────────────────────────

@router.post("/btcpay")
async def btcpay_webhook(
    request: Request,
    btcpay_sig: str = Header(None, alias="BTCPay-Sig"),
):
    raw_body = await request.body()

    # Verify HMAC signature from BTCPay
    if not verify_btcpay_webhook(raw_body, btcpay_sig or ""):
        logger.warning("BTCPay webhook: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    event_type    = data.get("type", "")
    btcpay_id     = data.get("invoiceId", "")

    # Map event type to status
    TYPE_TO_STATUS = {
        "InvoiceSettled":         "Complete",
        "InvoicePaymentSettled":  "Processing",
        "InvoiceExpired":         "Expired",
        "InvoiceInvalid":         "Invalid",
        "InvoiceCreated":         "New",
        "InvoiceProcessing":      "Processing",
    }
    btcpay_status = TYPE_TO_STATUS.get(event_type, "New")

    logger.info(f"BTCPay webhook: {event_type} | invoice {btcpay_id} | status {btcpay_status}")

    # Only act on terminal states
    our_status = BTCPAY_STATUS_MAP.get(btcpay_status)
    if not our_status or our_status == "pending":
        return {"received": True, "action": "none"}

    async with AsyncSessionLocal() as db:
        # Find the crypto invoice record
        inv_result = await db.execute(
            select(CryptoInvoice).where(CryptoInvoice.btcpay_invoice_id == btcpay_id)
        )
        inv_rec = inv_result.scalar_one_or_none()

        if not inv_rec:
            logger.warning(f"BTCPay webhook: no CryptoInvoice found for {btcpay_id}")
            return {"received": True, "action": "not_found"}

        # Update crypto invoice
        inv_rec.status = btcpay_status
        if our_status == "paid":
            inv_rec.settled_at = datetime.now(timezone.utc)

        # Update parent order
        order_result = await db.execute(select(Order).where(Order.id == inv_rec.order_id))
        order = order_result.scalar_one_or_none()

        should_create_shopify = False
        # != paid (not == pending): a customer who declines once and
        # successfully retries on the same order must still be able to reach
        # "paid" — restricting this to pending-only silently dropped that
        # exact sequence (confirmed: a real WPay 2D order sat "failed" for
        # over an hour after a successful retry, until an admin manually
        # caught it). Same fix applied identically across every webhook/poll
        # handler in this file and tasks/celery_app.py.
        if order and order.payment_status != PaymentStatus.paid:
            order.payment_status = PaymentStatus(our_status)
            if our_status == "paid":
                order.paid_at = datetime.now(timezone.utc)
                order.payment_notes = f"BTCPay invoice {btcpay_id} settled."
                logger.info(f"✅ Crypto payment confirmed: order {order.id}")
                should_create_shopify = True
            elif our_status == "expired":
                order.payment_notes = f"BTCPay invoice {btcpay_id} expired."
            elif our_status == "failed":
                order.payment_notes = f"BTCPay invoice {btcpay_id} invalid/failed."

        await db.commit()

    # Shopify order creation happens outside the DB transaction so a Shopify
    # failure doesn't roll back our own "paid" status.
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == inv_rec.order_id)
                .options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, send_email=False, label="crypto")

    return {"received": True, "action": our_status}


# ─── POST /webhooks/shopify-paid ──────────────────────────────────────────────

@router.post("/shopify-paid")
async def shopify_paid_webhook(request: Request):
    """
    Fired by Shopify when an order is paid on any of the bridge stores (MPC,
    FRT Chek, ONEPEPSCHECK, TWE Chek). Matches the Shopify order back to our
    DB record by the ORD-XXXXXXXX reference the bridge adds to note_attributes,
    and marks it paid.

    If no match is found (e.g. order placed directly on Shopify, not through
    our custom checkout), the webhook is silently ignored — the Revenue tab's
    live Shopify fetch still shows it.
    """
    raw_body      = await request.body()
    shopify_hmac  = request.headers.get("X-Shopify-Hmac-Sha256", "")
    shop_domain   = request.headers.get("X-Shopify-Shop-Domain", "")

    if not _verify_shopify_hmac(raw_body, shopify_hmac, shop_domain):
        logger.warning(f"Shopify webhook: invalid HMAC from {shop_domain}")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        order_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    shopify_order_id = str(order_data.get("id", ""))
    shopify_name     = order_data.get("name", "")      # e.g. "#1099FT"
    email            = (order_data.get("email") or "").lower()
    total_price      = order_data.get("total_price", "0")

    logger.info(
        f"Shopify webhook: paid order {shopify_name} ({shopify_order_id}) "
        f"from {shop_domain} — {email} / ${total_price}"
    )

    # Look for our ref in note_attributes (the bridge worker sets it as
    # `_src` with a value like "peptideslab.ca | ref:ORD-K3M9P2QA").
    bridge_order_id = None
    for attr in (order_data.get("note_attributes") or []):
        if attr.get("name") == "_src":
            m = re.search(r"ref:(ORD-[A-Z0-9]+)", attr.get("value", ""))
            if m:
                bridge_order_id = m.group(1)
                break

    async with AsyncSessionLocal() as db:
        if bridge_order_id:
            # Exact match by order ID — most reliable
            stmt = select(Order).where(Order.id == bridge_order_id)
        else:
            # Fallback: match by email + total + pending + card
            stmt = (
                select(Order)
                .where(
                    Order.email == email,
                    Order.total == Decimal(str(total_price)),
                    Order.payment_status == PaymentStatus.pending,
                    Order.payment_method == PaymentMethod.card,
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        result = await db.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            logger.info(
                f"No matching DB order for {shopify_name} "
                f"(ref:{bridge_order_id or 'none'}) "
                f"— likely placed directly on Shopify, not via custom checkout"
            )
            return {"received": True, "action": "no_match"}

        if order.payment_status == PaymentStatus.paid:
            logger.info(f"Order {order.id} already marked paid — webhook replay, ignoring")
            return {"received": True, "action": "already_paid"}

        order.payment_status = PaymentStatus.paid
        order.paid_at        = datetime.now(timezone.utc)
        order.payment_ref    = shopify_order_id
        order.payment_notes  = f"Matched to Shopify order {shopify_name} on {shop_domain}"
        await db.commit()

        logger.info(f"✅ Order {order.id} matched and marked paid")
        return {"received": True, "action": "matched", "orderId": order.id}


def _verify_shopify_hmac(body: bytes, hmac_header: str, shop_domain: str) -> bool:
    """
    Each Shopify store has its own webhook signing secret, shown on each
    store's Shopify admin → Settings → Notifications → Webhooks page.

    Configure stores in .env using paired vars with any prefix:
        MPC_CHECKOUT_SHOP=mpc-store.myshopify.com
        MPC_WEBHOOK_SECRET=<secret>

        US_CHECKOUT_SHOP=76vpwc-g9.myshopify.com
        US_WEBHOOK_SECRET=<secret>

        STORE_X_CHECKOUT_SHOP=...
        STORE_X_WEBHOOK_SECRET=...

    All pairs ending in `_CHECKOUT_SHOP` + `_WEBHOOK_SECRET` are auto-detected.
    Adding a new store = add 2 env vars and restart, no code change.
    """
    if not hmac_header:
        return False

    secrets_by_shop = {}
    # Walk all settings fields ending in _CHECKOUT_SHOP, find the matching _WEBHOOK_SECRET
    for attr in dir(settings):
        if not attr.endswith("_CHECKOUT_SHOP"):
            continue
        prefix = attr[: -len("_CHECKOUT_SHOP")]
        secret_attr = f"{prefix}_WEBHOOK_SECRET"
        shop   = getattr(settings, attr,        "")
        secret = getattr(settings, secret_attr, "")
        if shop and secret:
            secrets_by_shop[shop] = secret

    secret = secrets_by_shop.get(shop_domain, "")
    if not secret:
        logger.warning(f"No webhook secret configured for shop: {shop_domain}")
        return False

    expected = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, hmac_header)


@router.post("/nowpayments")
async def nowpayments_ipn(
    request: Request,
    x_nowpayments_sig: str = Header(None, alias="x-nowpayments-sig"),
):
    raw_body = await request.body()

    if not verify_nowpayments_ipn(raw_body, x_nowpayments_sig or ""):
        logger.warning("NowPayments IPN: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid IPN signature.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    np_payment_id = str(data.get("payment_id", ""))
    np_status     = data.get("payment_status", "")
    order_id      = data.get("order_id", "")
    price_amount  = Decimal(str(data.get("price_amount", "0")))
    actually_paid = Decimal(str(data.get("actually_paid_amount", data.get("actually_paid", "0"))))
    pay_currency  = data.get("pay_currency", "")

    logger.info(f"NowPayments IPN: payment {np_payment_id} | status={np_status} | order={order_id} | paid={actually_paid} {pay_currency}")

    our_status = NOWPAYMENTS_STATUS_MAP.get(np_status)
    if not our_status or our_status == "pending":
        async with AsyncSessionLocal() as db:
            inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
            inv_rec = inv_result.scalar_one_or_none()
            if inv_rec:
                inv_rec.np_payment_id = np_payment_id
                inv_rec.status        = np_status
                inv_rec.coin          = pay_currency
                await db.commit()
        return {"received": True, "action": "none"}

    # Underpayment — trust NowPayments' own status. They emit "partially_paid"
    # when the customer sent less than the invoice required (within their
    # tolerance settings). DO NOT compare actually_paid (crypto) to
    # price_amount (fiat) — different units, comparison is meaningless.
    if np_status == "partially_paid":
        pay_amount = Decimal(str(data.get("pay_amount", "0")))
        underpay_pct = 0.0
        if pay_amount > 0 and actually_paid > 0:
            underpay_pct = float((pay_amount - actually_paid) / pay_amount * 100)
        logger.warning(
            f"⚠️  NowPayments order {order_id} partially paid "
            f"({actually_paid}/{pay_amount} {pay_currency}, ~{underpay_pct:.2f}% short)"
        )
        # Approximate fiat equivalent for the admin "Amount received" column
        received_fiat_approx = Decimal("0")
        if pay_amount > 0:
            received_fiat_approx = (price_amount * actually_paid / pay_amount).quantize(Decimal("0.01"))
        async with AsyncSessionLocal() as db:
            inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
            inv_rec = inv_result.scalar_one_or_none()
            if inv_rec:
                inv_rec.np_payment_id = np_payment_id
                inv_rec.received_fiat = received_fiat_approx
                inv_rec.status        = "underpaid"
                inv_rec.coin          = pay_currency
            await db.commit()
        return {"received": True, "action": "underpaid", "underpay_pct": round(underpay_pct, 2)}

    async with AsyncSessionLocal() as db:
        inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
        inv_rec = inv_result.scalar_one_or_none()

        if not inv_rec:
            logger.warning(f"NowPayments IPN: no invoice found for order {order_id}")
            return {"received": True, "action": "not_found"}

        inv_rec.np_payment_id = np_payment_id
        inv_rec.status        = np_status
        inv_rec.coin          = pay_currency
        if our_status == "paid":
            inv_rec.settled_at = datetime.now(timezone.utc)

        order_result = await db.execute(select(Order).where(Order.id == order_id))
        order = order_result.scalar_one_or_none()

        should_create_shopify = False
        if order and order.payment_status != PaymentStatus.paid:  # see BTCPay handler note above
            order.payment_status = PaymentStatus(our_status)
            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"NowPayments {np_payment_id} finished ({pay_currency})."
                logger.info(f"✅ Altcoin payment confirmed: order {order.id}")
                should_create_shopify = True
            elif our_status == "expired":
                order.payment_notes = f"NowPayments {np_payment_id} expired."
            elif our_status == "failed":
                order.payment_notes = f"NowPayments {np_payment_id} failed."
        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, send_email=False, label="altcoin")

    return {"received": True, "action": our_status}

# ─── POST /webhooks/pymtz ─────────────────────────────────────────────────────
# Based on https://pymtz.co/api-guide.html (the simpler page). pymtz's two doc
# pages disagree on the event name — api-guide.html shows "payment.succeeded",
# api-docs.html shows "payment.completed". We accept BOTH so we're not at the
# mercy of which one pymtz's production actually fires.
#
# FORGERY PROTECTION
# pymtz documents webhook signing but never tells us how to get the signing
# secret, so we can't verify HMAC signatures. Instead, before marking ANY
# order paid based on a webhook, we call pymtz's authenticated GET
# /payments/{payment_id} endpoint using our API key. The forger doesn't have
# our API key, so they can't fake pymtz's response — and pymtz will only
# report status="completed" for payments that were actually paid. A forged
# webhook gets rejected because pymtz itself says the payment isn't complete.
#
# What this still gives up vs api-docs.html coverage:
#   • No failure/refund handler → declines stay `pending` until expiry;
#     refunds get flipped manually in /peps-admin-2026.

@router.post("/pymtz")
async def pymtz_webhook(request: Request):
    """
    pymtz webhook handler — docs-literal per api-guide.html, plus an
    authenticated cross-verify against pymtz to defeat webhook forgery.

    Documented payload (their Node example):
        { "event": "payment.succeeded", "data": { ... } }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    # Webhook events may use the "event" key (api-guide.html style) or the
    # "type" key (api-docs.html style). Read both.
    event = body.get("event") or body.get("type") or ""
    data  = body.get("data", {}) or {}

    # Accept both event-name styles. Anything else → log + ack.
    if event not in ("payment.succeeded", "payment.completed"):
        logger.info(f"pymtz webhook: ignoring event {event!r}")
        return {"received": True}

    payment_id = str(data.get("payment_id") or data.get("id") or "")
    metadata   = data.get("metadata", {}) or {}
    order_id   = metadata.get("order_id", "")

    # Recover order via payment_ref if metadata didn't carry order_id.
    if not order_id and payment_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Order).where(Order.payment_ref == payment_id))
            match = res.scalar_one_or_none()
            if match:
                order_id = match.id

    logger.info(
        f"pymtz webhook: event={event} payment={payment_id} order={order_id}"
    )

    if not order_id:
        logger.warning(f"pymtz webhook: could not resolve order for payment {payment_id}")
        return {"received": True}

    # ── Pre-read order for currency + idempotency ────────────────────────────
    # We do this BEFORE the cross-verify HTTP call so we (a) can skip pymtz
    # entirely if the order is already paid, and (b) know which pymtz account
    # (CA vs US) to authenticate against.
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order_pre = result.scalar_one_or_none()
    if not order_pre:
        logger.warning(f"pymtz webhook: no order found for {order_id}")
        return {"received": True}
    if order_pre.payment_status == PaymentStatus.paid:
        return {"received": True}   # already paid — skip

    # ── Forgery protection #1: payment_id must belong to THIS order ───────────
    # When /api/checkout/card creates a pymtz payment, we stash the returned
    # pymtz payment ID on order.payment_ref. A legitimate webhook for this
    # order must therefore have payment_id == order.payment_ref. This blocks
    # the "use someone else's paid payment_id to mark other orders paid"
    # attack — the forger would need the exact pymtz payment ID that pymtz
    # created for THIS order, which they could only know if they were the
    # one who initiated checkout for it.
    if not payment_id:
        logger.warning(f"pymtz webhook: REJECTED — no payment_id for order {order_id}")
        return {"received": True}
    if not order_pre.payment_ref:
        logger.warning(
            f"pymtz webhook: REJECTED — order {order_id} has no payment_ref "
            f"(no pymtz payment ever created for it?)"
        )
        return {"received": True}
    if order_pre.payment_ref != payment_id:
        logger.warning(
            f"pymtz webhook: REJECTED — payment_id mismatch (webhook payment_id="
            f"{payment_id}, order.payment_ref={order_pre.payment_ref!r}). Order "
            f"{order_id} likely targeted by forgery using another order's payment ID."
        )
        return {"received": True}

    # ── Forgery protection #2: cross-verify with pymtz API ────────────────────
    # Confirms the payment actually completed by asking pymtz directly using
    # our authenticated API key. Catches the (now narrow) cases where the
    # webhook is forged for a payment_id that genuinely belongs to this order
    # but hasn't actually completed yet.
    try:
        pymtz_country = "US" if (order_pre.currency or "").upper() == "USD" else "CA"
        from services.pymtz import PymtzClient
        pymtz_payment = await PymtzClient(country=pymtz_country).get_payment(payment_id)
    except Exception as e:
        logger.warning(
            f"pymtz webhook: cross-verify call failed for payment={payment_id} "
            f"order={order_id}: {e}"
        )
        return {"received": True}

    pymtz_status = str(pymtz_payment.get("status") or "").lower()
    if pymtz_status not in ("completed", "succeeded", "paid"):
        logger.warning(
            f"pymtz webhook: REJECTED — pymtz reports status={pymtz_status!r} for "
            f"payment={payment_id} order={order_id}. Likely forged or stale."
        )
        return {"received": True}

    # ── Mark paid (idempotent — second guard in case of race) ────────────────
    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            return {"received": True}

        if order.payment_status == PaymentStatus.paid:
            return {"received": True}   # raced with another path — skip

        if order.payment_status != PaymentStatus.paid:  # see BTCPay handler note above
            order.payment_status = PaymentStatus.paid
            order.paid_at        = datetime.now(timezone.utc)
            order.payment_ref    = payment_id or order.payment_ref
            order.payment_notes  = f"pymtz {payment_id} succeeded (cross-verified)."
            should_create_shopify = True
            logger.info(f"✅ Card payment confirmed (pymtz): order {order.id}")
        await db.commit()

    # ── Downstream — Shopify create + affiliate webhook ──────────────────────
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, send_email=False, label="pymtz")

    return {"received": True}


# ─── POST /webhooks/wpay ──────────────────────────────────────────────────────
#
# WPay callback handler. WPay doesn't document HMAC signing for callbacks, so
# (same approach as pymtz) we protect against forgery two ways:
#   1. The transaction_id must belong to THIS order (order.payment_ref match)
#   2. Cross-verify the payment actually succeeded via WPay's own status API
#      before ever trusting the callback body's claimed status.
#
# WPay's callback docs show PHP-array-style output, implying a form POST
# rather than JSON — this handler accepts either.

@router.post("/wpay")
async def wpay_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    transaction_id = str(body.get("transaction_id") or body.get("client_transaction_id") or "")
    if not transaction_id:
        logger.warning("wpay webhook: no transaction_id in payload")
        return {"received": True}

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.payment_ref == transaction_id))
        order_pre = result.scalar_one_or_none()

    if not order_pre:
        logger.warning(f"wpay webhook: no order found for transaction_id={transaction_id}")
        return {"received": True}
    if order_pre.payment_status == PaymentStatus.paid:
        return {"received": True}   # already paid — skip

    # ── Forgery protection #1: transaction_id must belong to THIS order ──────
    if order_pre.payment_ref != transaction_id:
        logger.warning(
            f"wpay webhook: REJECTED — transaction_id mismatch (webhook={transaction_id}, "
            f"order.payment_ref={order_pre.payment_ref!r}). Order {order_pre.id} likely "
            f"targeted by forgery using another order's transaction ID."
        )
        return {"received": True}

    # ── Forgery protection #2: cross-verify with WPay directly ───────────────
    try:
        from services.wpay import WPayClient, normalize_status
        wpay_record = await WPayClient().verify_transaction(transaction_id)
    except Exception as e:
        logger.warning(f"wpay webhook: cross-verify call failed for txn={transaction_id}: {e}")
        return {"received": True}

    raw_status = str(wpay_record.get("status") or wpay_record.get("response") or "")
    wpay_status = normalize_status(raw_status)
    if wpay_status != "success":
        logger.warning(
            f"wpay webhook: REJECTED — WPay reports status={wpay_status!r} for "
            f"txn={transaction_id} order={order_pre.id}. Likely forged or stale."
        )
        return {"received": True}

    # ── Mark paid (idempotent — second guard in case of race) ────────────────
    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_pre.id))
        order  = result.scalar_one_or_none()
        if not order:
            return {"received": True}

        if order.payment_status == PaymentStatus.paid:
            return {"received": True}   # raced with another path — skip

        if order.payment_status != PaymentStatus.paid:  # see BTCPay handler note above
            order.payment_status = PaymentStatus.paid
            order.paid_at        = datetime.now(timezone.utc)
            order.payment_notes  = f"wpay {transaction_id} succeeded (cross-verified)."
            should_create_shopify = True
            logger.info(f"✅ Card payment confirmed (wpay): order {order.id}")
        await db.commit()

    # ── Downstream — Shopify create + affiliate webhook ──────────────────────
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_pre.id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, send_email=False, label="wpay")

    return {"received": True}


# ──────────────────────────────────────────────────────────────────────────────
# Stripe direct webhook handler
#
# Most events fire AFTER the synchronous /api/checkout/stripe_direct
# call already marked the order paid. The webhook serves as:
#   1. Secondary safety net if the synchronous response was lost
#   2. Notification for after-the-fact events (refunds from dashboard, etc.)
#
# Stripe events we care about:
#   payment_intent.succeeded             — charge completed
#   payment_intent.payment_failed        — charge failed
#   payment_intent.canceled              — charge canceled
#   charge.refunded                      — refund issued
#   charge.dispute.created               — chargeback opened
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/stripe_direct")
async def stripe_direct_webhook(request: Request):
    """
    Verify Stripe webhook signature (HMAC-SHA256 with timestamp + raw body),
    decode the event, and act on it. Idempotent.
    """
    from services.stripe_direct import StripeDirectClient

    raw_body = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    client = StripeDirectClient()
    if not client.verify_webhook(raw_body, signature):
        logger.warning("[stripe_direct webhook] invalid signature — rejecting")
        raise HTTPException(401, "Invalid signature")

    try:
        event = json.loads(raw_body)
    except Exception:
        logger.error(f"[stripe_direct webhook] invalid JSON: {raw_body[:200]!r}")
        raise HTTPException(400, "Invalid JSON")

    event_type = event.get("type", "")
    payload    = event.get("data", {}).get("object", {}) or {}
    pi_id      = payload.get("id") or payload.get("payment_intent", "")

    logger.info(f"[stripe_direct webhook] event={event_type} pi={pi_id}")

    # Find the order by stored payment_ref. We store as "pi_<intent_id>" so
    # the existing classifier can recognize it; match accordingly.
    if not pi_id:
        return {"received": True, "action": "no_pi"}

    async with AsyncSessionLocal() as db:
        # payment_ref stored as "pi_<the-id>" — Stripe's IDs already start
        # with "pi_" so this looks like e.g. "pi_pi_3xxxxx" depending on
        # how we wrote it. Match by suffix to be safe.
        suffix = pi_id.replace("pi_", "")
        result = await db.execute(
            select(Order).where(Order.payment_ref.ilike(f"%{suffix}%"))
        )
        order = result.scalar_one_or_none()

        if not order:
            logger.info(f"[stripe_direct webhook] no order found for pi={pi_id} (event={event_type})")
            return {"received": True, "action": "no_order"}

        # ── Handle each event type ───────────────────────────────────────────
        if event_type == "payment_intent.succeeded":
            if order.payment_status != PaymentStatus.paid:
                order.payment_status = PaymentStatus.paid
                order.paid_at        = datetime.now(timezone.utc)
                order.payment_notes  = (
                    (order.payment_notes or "") +
                    f" | [webhook] payment_intent.succeeded at {datetime.now(timezone.utc).isoformat()}"
                )[:1000]
                await db.commit()
                logger.info(f"✅ [stripe_direct webhook] order {order.id} marked paid (sync had failed)")
            else:
                logger.info(f"[stripe_direct webhook] order {order.id} already paid — webhook is duplicate")

        elif event_type == "payment_intent.payment_failed":
            if order.payment_status == PaymentStatus.pending:
                order.payment_status = PaymentStatus.failed
                err = payload.get("last_payment_error") or {}
                order.payment_notes  = (
                    (order.payment_notes or "") +
                    f" | [webhook] payment_intent.payment_failed: {err.get('message', 'unknown')[:200]}"
                )[:1000]
                await db.commit()
                logger.info(f"[stripe_direct webhook] order {order.id} marked failed")

        elif event_type == "payment_intent.canceled":
            order.payment_status = PaymentStatus.failed
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] payment_intent.canceled at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[stripe_direct webhook] order {order.id} marked failed (canceled)")

        elif event_type == "charge.refunded":
            order.payment_status = PaymentStatus.refunded
            refund_amount = (payload.get("amount_refunded") or 0) / 100.0
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] charge.refunded amount=${refund_amount:.2f}"
            )[:1000]
            await db.commit()
            logger.info(f"[stripe_direct webhook] order {order.id} marked refunded (${refund_amount:.2f})")

        elif event_type == "charge.dispute.created":
            # Chargeback — log but don't change status. The merchant team
            # needs to know but the order is still "paid" until the dispute resolves.
            dispute_amount = (payload.get("amount") or 0) / 100.0
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | ⚠️ [webhook] DISPUTE OPENED amount=${dispute_amount:.2f} reason={payload.get('reason', 'unknown')}"
            )[:1000]
            await db.commit()
            logger.warning(f"⚠️ [stripe_direct webhook] CHARGEBACK on order {order.id}: ${dispute_amount:.2f}")

        else:
            logger.info(f"[stripe_direct webhook] unhandled event type: {event_type}")

    return {"received": True, "action": event_type, "order_id": order.id if order else None}


# ──────────────────────────────────────────────────────────────────────────────
# Onramp via WordPress + 2530gateway plugin (legacy path, kept dormant)
# ──────────────────────────────────────────────────────────────────────────────

def _verify_wc_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    WooCommerce webhooks sign the raw body with HMAC-SHA256 + base64 encoded.
    Header: X-WC-Webhook-Signature: <base64-hmac-sha256-of-body>
    Secret: the value you set in WP admin → WooCommerce → Settings → Advanced
            → Webhooks → (your webhook) → Secret
    """
    if not secret or not signature:
        return False
    try:
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(expected, signature.strip())
    except Exception:
        return False


@router.post("/onramp_wp")
async def onramp_wp_webhook(
    request: Request,
    x_wc_webhook_signature: str = Header(None, alias="x-wc-webhook-signature"),
    x_wc_webhook_topic:     str = Header(None, alias="x-wc-webhook-topic"),
):
    """
    Receive WooCommerce webhook from the WP site running the 2530gateway plugin.
    Topics we care about: `order.updated`, `order.created`. We act on status
    transitions to "processing" or "completed" (both = paid in WC parlance).
    """
    raw_body = await request.body()

    secret = getattr(settings, "ONRAMP_WP_WEBHOOK_SECRET", "") or ""
    if secret:
        if not _verify_wc_signature(raw_body, x_wc_webhook_signature or "", secret):
            logger.warning("onramp_wp webhook: invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    elif settings.ENVIRONMENT == "production":
        logger.warning("onramp_wp webhook: ONRAMP_WP_WEBHOOK_SECRET not set in production")
        raise HTTPException(status_code=401, detail="Webhook secret not configured.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    wc_order_id = data.get("id", "")
    wc_status   = (data.get("status") or "").lower()

    # Pull our external order_id out of meta_data
    order_id = ""
    for meta in (data.get("meta_data") or []):
        if meta.get("key") in ("_external_order_id", "external_order_id"):
            order_id = str(meta.get("value") or "")
            break

    # Fallback — match by stored payment_ref `wc:<id>`
    if not order_id and wc_order_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(Order).where(Order.payment_ref == f"wc:{wc_order_id}")
            )
            ord_match = res.scalar_one_or_none()
            if ord_match:
                order_id = ord_match.id

    logger.info(
        f"onramp_wp webhook: topic={x_wc_webhook_topic} wc_order={wc_order_id} "
        f"wc_status={wc_status} order={order_id}"
    )

    if not order_id:
        logger.warning(f"onramp_wp webhook: could not resolve order for WC order {wc_order_id}")
        return {"received": True, "action": "no_order"}

    from services.onramp_wp import WC_STATUS_MAP
    our_status = WC_STATUS_MAP.get(wc_status)
    if not our_status or our_status == "pending":
        return {"received": True, "action": "none", "status": wc_status}

    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            logger.warning(f"onramp_wp webhook: no order found for {order_id}")
            return {"received": True, "action": "not_found"}

        if order.payment_status != PaymentStatus.paid:  # see BTCPay handler note above
            order.payment_status = PaymentStatus(our_status)
            order.payment_ref    = f"wc:{wc_order_id}"
            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"onramp_wp WC #{wc_order_id} {wc_status}."
                should_create_shopify = True
                logger.info(f"✅ Card payment confirmed (onramp_wp): order {order.id} / WC #{wc_order_id}")
            elif our_status == "failed":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} failed."
            elif our_status == "cancelled":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} cancelled."
            elif our_status == "refunded":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} refunded."
        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, send_email=False, label="onramp_wp")

    return {"received": True, "action": "processed", "order_id": order_id, "status": our_status}


# ──────────────────────────────────────────────────────────────────────────────
# WPay 2D via the same WordPress site (separate gateway ID, same WC install)
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/wpay_2d")
async def wpay_2d_webhook(
    request: Request,
    x_wc_webhook_signature: str = Header(None, alias="x-wc-webhook-signature"),
    x_wc_webhook_topic:     str = Header(None, alias="x-wc-webhook-topic"),
):
    """
    Receive WooCommerce webhook from the WP site's WPay Channels plugin
    ("WPay 2D" gateway). Same site as onramp_wp, same WC webhook signature
    scheme, just a distinct webhook subscription in WP admin pointing here
    instead of /webhooks/onramp_wp — WC order IDs are unique per site
    regardless of which gateway processed them, so there's no ambiguity
    matching orders back even though both flows share one WordPress install.
    """
    raw_body = await request.body()

    secret = getattr(settings, "WPAY_WP_WEBHOOK_SECRET", "") or ""
    if secret:
        if not _verify_wc_signature(raw_body, x_wc_webhook_signature or "", secret):
            logger.warning("wpay_2d webhook: invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    elif settings.ENVIRONMENT == "production":
        logger.warning("wpay_2d webhook: WPAY_WP_WEBHOOK_SECRET not set in production")
        raise HTTPException(status_code=401, detail="Webhook secret not configured.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    wc_order_id = data.get("id", "")
    wc_status   = (data.get("status") or "").lower()

    order_id = ""
    for meta in (data.get("meta_data") or []):
        if meta.get("key") in ("_external_order_id", "external_order_id"):
            order_id = str(meta.get("value") or "")
            break

    if not order_id and wc_order_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(Order).where(Order.payment_ref == f"wc:{wc_order_id}")
            )
            ord_match = res.scalar_one_or_none()
            if ord_match:
                order_id = ord_match.id

    logger.info(
        f"wpay_2d webhook: topic={x_wc_webhook_topic} wc_order={wc_order_id} "
        f"wc_status={wc_status} order={order_id}"
    )

    if not order_id:
        logger.warning(f"wpay_2d webhook: could not resolve order for WC order {wc_order_id}")
        return {"received": True, "action": "no_order"}

    from services.wpay_wp import WC_STATUS_MAP
    our_status = WC_STATUS_MAP.get(wc_status)
    if not our_status or our_status == "pending":
        return {"received": True, "action": "none", "status": wc_status}

    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            logger.warning(f"wpay_2d webhook: no order found for {order_id}")
            return {"received": True, "action": "not_found"}

        if order.payment_status != PaymentStatus.paid:  # see BTCPay handler note above — this exact guard is what dropped ORD-CQ9HOGTR
            order.payment_status = PaymentStatus(our_status)
            order.payment_ref    = f"wc:{wc_order_id}"
            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"wpay_2d WC #{wc_order_id} {wc_status}."
                should_create_shopify = True
                logger.info(f"✅ Card payment confirmed (wpay_2d): order {order.id} / WC #{wc_order_id}")
            elif our_status == "failed":
                order.payment_notes = f"wpay_2d WC #{wc_order_id} failed."
            elif our_status == "cancelled":
                order.payment_notes = f"wpay_2d WC #{wc_order_id} cancelled."
            elif our_status == "refunded":
                order.payment_notes = f"wpay_2d WC #{wc_order_id} refunded."
        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, label="wpay_2d")

    return {"received": True, "action": "processed", "order_id": order_id, "status": our_status}
