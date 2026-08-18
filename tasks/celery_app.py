"""
Celery async task worker.

Start worker:  celery -A tasks.celery_app worker --loglevel=info
Start beat:    celery -A tasks.celery_app beat --loglevel=info

Tasks:
  - poll_and_match_interac   → runs every INTERAC_POLL_INTERVAL seconds
  - expire_old_orders        → runs every hour
  - check_btcpay_invoice     → called on-demand after crypto order creation
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery.schedules import crontab

from config import settings

logger = logging.getLogger(__name__)

app = Celery("checkout", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_max_tasks_per_child=200,
)

# ─── Periodic schedule ───────────────────────────────────────────────────────
app.conf.beat_schedule = {
    # Interac auto-matching DISABLED — manual mode via admin panel
    # Uncomment when Gmail OAuth is ready:
    # "poll-interac-emails": {
    #     "task":     "tasks.celery_app.poll_and_match_interac",
    #     "schedule": settings.INTERAC_POLL_INTERVAL,
    # },
    "expire-old-orders": {
        "task":     "tasks.celery_app.expire_old_orders",
        "schedule": crontab(minute=0),
    },
}


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─── Task: Interac email polling + matching ───────────────────────────────────
@app.task(name="tasks.celery_app.poll_and_match_interac", bind=True, max_retries=3)
def poll_and_match_interac(self):
    """Poll Gmail for Interac e-Transfer notifications and match to orders."""
    from services.interac_watcher import poll_interac_emails

    emails = poll_interac_emails()
    if not emails:
        return {"matched": 0, "unmatched": 0, "skipped": 0}

    return _run_async(_match_emails_to_orders(emails))


async def _match_emails_to_orders(emails: list[dict]) -> dict:
    from database import AsyncSessionLocal
    from models.order import Order, InteracPayment, PaymentStatus
    from sqlalchemy import select

    stats = {"matched": 0, "unmatched": 0, "skipped": 0}

    async with AsyncSessionLocal() as db:
        for email in emails:
            gmail_id = email["gmail_message_id"]

            # Check if already processed (dedup by gmail message ID)
            existing = await db.execute(
                select(InteracPayment).where(
                    InteracPayment.raw_email_id == gmail_id
                )
            )
            if existing.scalar_one_or_none():
                stats["skipped"] += 1
                continue

            order_id = email.get("order_id")
            amount   = email.get("amount")

            if not order_id:
                # No order ID found — create unmatched record for manual review
                logger.warning(f"Unmatched Interac email: {email['subject']} | {email['body_snippet'][:100]}")
                new_payment = InteracPayment(
                    order_id        = None,
                    expected_amount = amount or 0,
                    sender_email    = email["sender"],
                    raw_email_id    = gmail_id,
                    status          = "unmatched",
                    notes           = f"Subject: {email['subject']}\nSnippet: {email['body_snippet'][:300]}",
                )
                db.add(new_payment)
                stats["unmatched"] += 1
                continue

            # Look up the order
            order_result = await db.execute(
                select(Order).where(
                    Order.id == order_id,
                    Order.payment_method == "interac",
                    Order.payment_status == PaymentStatus.pending,
                )
            )
            order = order_result.scalar_one_or_none()

            if not order:
                logger.warning(f"Order {order_id} not found or not pending Interac.")
                stats["unmatched"] += 1
                continue

            # Optional: validate amount matches (allow ±$1 tolerance for bank fees)
            expected = float(order.total)
            if amount and abs(amount - expected) > 1.00:
                logger.warning(
                    f"Amount mismatch for {order_id}: expected ${expected}, got ${amount}"
                )
                # Still flag as unmatched for manual review
                interac_rec = InteracPayment(
                    order_id        = order_id,
                    expected_amount = expected,
                    sender_email    = email["sender"],
                    raw_email_id    = gmail_id,
                    status          = "unmatched",
                    notes           = f"Amount mismatch: received ${amount}, expected ${expected}",
                )
                db.add(interac_rec)
                stats["unmatched"] += 1
                continue

            # ✅ Match confirmed — update order
            order.payment_status = PaymentStatus.paid
            order.paid_at        = datetime.now(timezone.utc)
            order.payment_notes  = f"Interac received from {email['sender']}"

            interac_rec = InteracPayment(
                order_id        = order_id,
                expected_amount = expected,
                sender_email    = email["sender"],
                matched_at      = datetime.now(timezone.utc),
                raw_email_id    = gmail_id,
                status          = "matched",
            )
            db.add(interac_rec)
            stats["matched"] += 1
            logger.info(f"✅ Interac matched: {order_id} — ${amount}")

        await db.commit()

    return stats


# ─── Task: Expire unpaid orders ──────────────────────────────────────────────
@app.task(name="tasks.celery_app.expire_old_orders")
def expire_old_orders():
    return _run_async(_do_expire_orders())


async def _do_expire_orders():
    from database import AsyncSessionLocal
    from models.order import Order, PaymentStatus
    from sqlalchemy import select, update

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.ORDER_EXPIRY_HOURS)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Order)
            .where(
                Order.payment_status == PaymentStatus.pending,
                Order.created_at < cutoff,
            )
            .values(payment_status=PaymentStatus.expired)
        )
        await db.commit()
        count = result.rowcount
        if count:
            logger.info(f"Expired {count} old pending orders.")
        return {"expired": count}


# ─── Task: Verify BTCPay invoice status (on-demand poll) ─────────────────────
@app.task(
    name="tasks.celery_app.check_btcpay_invoice",
    bind=True,
    max_retries=10,
    default_retry_delay=60,   # retry every 60s up to 10 times = 10 min window
)
def check_btcpay_invoice(self, order_id: str, invoice_id: str):
    """
    Poll BTCPay for invoice status. Used as fallback if webhook delivery fails.
    Retries automatically; stops when invoice settles or expires.
    """
    return _run_async(_do_check_btcpay(self, order_id, invoice_id))


async def _do_check_btcpay(task, order_id: str, invoice_id: str):
    from database import AsyncSessionLocal
    from models.order import Order, CryptoInvoice, PaymentStatus
    from services.btcpay import BTCPayClient, BTCPAY_STATUS_MAP
    from sqlalchemy import select
    from datetime import timezone

    client = BTCPayClient()
    invoice = await client.get_invoice(invoice_id)
    btcpay_status = invoice.get("status", "New")
    our_status    = BTCPAY_STATUS_MAP.get(btcpay_status, "pending")

    if our_status == "pending":
        # Not settled yet, retry
        raise task.retry()

    async with AsyncSessionLocal() as db:
        order_result = await db.execute(select(Order).where(Order.id == order_id))
        order = order_result.scalar_one_or_none()

        # != paid (not == pending): a customer who declines once and
        # successfully retries on the same order must still reach "paid" —
        # pending-only silently dropped that sequence (see the identical fix
        # + note in routes/webhooks.py). Same change applied to every poll
        # task in this file.
        if order and order.payment_status != PaymentStatus.paid:
            order.payment_status = PaymentStatus(our_status)
            if our_status == "paid":
                order.paid_at = datetime.now(timezone.utc)

            invoice_result = await db.execute(
                select(CryptoInvoice).where(CryptoInvoice.btcpay_invoice_id == invoice_id)
            )
            inv_rec = invoice_result.scalar_one_or_none()
            if inv_rec:
                inv_rec.status = btcpay_status

            await db.commit()
            logger.info(f"BTCPay poll updated order {order_id} → {our_status}")

    return {"order_id": order_id, "status": our_status}


# ─── Task: Post-payment hooks (Shopify create + affiliate webhook) ───────────
# For synchronous card processors (e.g. Stripe Direct) whose charge
# already succeeded before this fires — moved OFF the customer-facing
# checkout response so a slow Shopify/affiliate call doesn't add latency to
# the customer's checkout. Errors are logged and swallowed per-hook, same as
# the inline try/except behavior this replaces — no retry, since a failure
# here was never retried before either.
@app.task(name="tasks.celery_app.run_post_payment_hooks")
def run_post_payment_hooks(order_id: str, processor_label: str = ""):
    return _run_async(_do_post_payment_hooks(order_id, processor_label))


async def _do_post_payment_hooks(order_id: str, processor_label: str):
    from database import AsyncSessionLocal
    from models.order import Order
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from services.order_finalize import finalize_paid_order

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        order = result.scalar_one_or_none()

        if not order:
            logger.warning(f"[post-payment-hooks] order {order_id} not found")
            return {"order_id": order_id, "status": "not_found"}

        # send_email=False preserves this task's existing behavior — Stripe
        # Direct never sent a confirmation email from this path.
        await finalize_paid_order(order, db, send_email=False, label=processor_label)

    return {"order_id": order_id, "status": "done"}


# ─── Task: Verify WPay payment status (on-demand poll) ───────────────────────
@app.task(
    name="tasks.celery_app.check_wpay_payment",
    bind=True,
    max_retries=10,
    default_retry_delay=300,   # retry every 5 min up to 10 times — WPay's own
                                # docs say the fetch_record run window should
                                # be > 45 min, which the initial countdown
                                # (set by the checkout route) already covers.
)
def check_wpay_payment(self, order_id: str, transaction_id: str):
    """
    Poll WPay for transaction status. Fallback if the callback_url webhook
    delivery fails or never arrives. Retries automatically; stops once the
    order is no longer pending.
    """
    return _run_async(_do_check_wpay(self, order_id, transaction_id))


async def _do_check_wpay(task, order_id: str, transaction_id: str):
    from database import AsyncSessionLocal
    from models.order import Order, PaymentStatus
    from services.wpay import WPayClient, normalize_status
    from sqlalchemy import select
    from datetime import timezone

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()

    # pending OR failed: an order already marked "failed" (e.g. a first
    # declined attempt) must still be pollable — otherwise a same-order
    # retry-and-succeed is silently missed by this fallback too, exactly
    # like the webhook guards fixed alongside this one.
    if not order or order.payment_status not in (PaymentStatus.pending, PaymentStatus.failed):
        return {"order_id": order_id, "status": "skipped"}

    try:
        record = await WPayClient().verify_transaction(transaction_id)
    except Exception as e:
        logger.warning(f"[wpay] poll failed for txn={transaction_id}: {e}")
        raise task.retry()

    raw_status  = str(record.get("status") or record.get("response") or "")
    wpay_status = normalize_status(raw_status)

    if wpay_status == "pending":
        raise task.retry()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()

        if order and order.payment_status != PaymentStatus.paid:  # see note above
            if wpay_status == "success":
                order.payment_status = PaymentStatus.paid
                order.paid_at        = datetime.now(timezone.utc)
                order.payment_notes  = f"wpay {transaction_id} succeeded (poll fallback)."
                await db.commit()
                logger.info(f"✅ WPay poll confirmed payment: order {order_id}")

                from sqlalchemy.orm import selectinload
                fresh_result = await db.execute(
                    select(Order).where(Order.id == order_id).options(selectinload(Order.items))
                )
                fresh_order = fresh_result.scalar_one_or_none()
                if fresh_order:
                    from services.order_finalize import finalize_paid_order
                    await finalize_paid_order(fresh_order, db, send_email=False, label="wpay-poll")
            else:
                order.payment_status = PaymentStatus.failed
                order.payment_notes  = f"wpay {transaction_id} declined (poll fallback): {raw_status}"
                await db.commit()
                logger.info(f"WPay poll: order {order_id} declined")

    return {"order_id": order_id, "status": wpay_status}


# ─── Task: Verify WPay 2D payment status (on-demand poll) ────────────────────
# Safety net for the wpay_2d webhook, which has shown intermittent invalid-
# signature failures on some deliveries for reasons not yet fully diagnosed.
# Polls the WooCommerce order's own status directly via REST API — no
# vendor-documented minimum wait like the WPay HPP fetch_record API, so this
# starts sooner and covers a shorter window.
@app.task(
    name="tasks.celery_app.check_wpay_2d_payment",
    bind=True,
    max_retries=10,
    default_retry_delay=180,   # retry every 3 min, ~30 min coverage total
)
def check_wpay_2d_payment(self, order_id: str, wc_order_id: int):
    return _run_async(_do_check_wpay_2d(self, order_id, wc_order_id))


async def _do_check_wpay_2d(task, order_id: str, wc_order_id: int):
    from database import AsyncSessionLocal
    from models.order import Order, PaymentStatus
    from services.wpay_wp import WPayWPClient, WC_STATUS_MAP
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()

    # pending OR failed — see identical note in _do_check_wpay above. This is
    # the fallback for the exact bug that dropped ORD-CQ9HOGTR: the primary
    # webhook guard alone wasn't enough since this poll task gave up on the
    # order the moment it saw "failed" too.
    if not order or order.payment_status not in (PaymentStatus.pending, PaymentStatus.failed):
        return {"order_id": order_id, "status": "skipped"}

    try:
        wc_order = await WPayWPClient().get_order(wc_order_id)
    except Exception as e:
        logger.warning(f"[wpay_2d] poll failed for WC order {wc_order_id}: {e}")
        raise task.retry()

    wc_status  = (wc_order.get("status") or "").lower()
    our_status = WC_STATUS_MAP.get(wc_status)

    if not our_status or our_status == "pending":
        raise task.retry()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        order = result.scalar_one_or_none()

        if order and order.payment_status != PaymentStatus.paid:  # see note above
            order.payment_status = PaymentStatus(our_status)
            order.payment_ref    = f"wc:{wc_order_id}"
            order.payment_notes  = f"wpay_2d WC #{wc_order_id} {wc_status} (poll fallback)."

            if our_status == "paid":
                order.paid_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"✅ WPay 2D poll confirmed payment: order {order_id}")

                # `order` already has .items eager-loaded from the query above.
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order, db, label="wpay_2d-poll")
            else:
                await db.commit()
                logger.info(f"WPay 2D poll: order {order_id} → {our_status}")

    return {"order_id": order_id, "status": our_status}
