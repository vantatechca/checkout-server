"""
Background task that periodically expires old pending orders.

Runs forever in a loop, scans for orders past their method-specific timeout,
and marks them as expired.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models.order import Order, PaymentStatus, PaymentMethod

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 600  # run every 10 minutes


def _expiration_minutes_for(method: PaymentMethod) -> int:
    return {
        PaymentMethod.card:    settings.ORDER_EXPIRY_CARD_MINUTES,
        PaymentMethod.crypto:  settings.ORDER_EXPIRY_CRYPTO_MINUTES,
        PaymentMethod.interac: settings.ORDER_EXPIRY_INTERAC_MINUTES,
        PaymentMethod.zelle:   settings.ORDER_EXPIRY_INTERAC_MINUTES,   # same as Interac (48h)
    }.get(method, 60)


async def expire_stale_orders_once() -> int:
    """Single pass: scan for pending orders past expiration, flip them."""
    now = datetime.now(timezone.utc)
    expired_count = 0

    async with AsyncSessionLocal() as db:
        for method in ():
            minutes = _expiration_minutes_for(method)
            cutoff = now - timedelta(minutes=minutes)

            stmt = select(Order).where(
                Order.payment_status == PaymentStatus.pending,
                Order.payment_method == method,
                Order.created_at < cutoff,
            )
            result = await db.execute(stmt)
            orders = result.scalars().all()

            for order in orders:
                order.payment_status = PaymentStatus.expired
                order.payment_notes = (
                    f"Auto-expired after {minutes} min of no payment "
                    f"({method.value})"
                )
                logger.info(f"Order {order.id} expired ({method.value}, age {minutes}min+)")
                expired_count += 1

        if expired_count > 0:
            await db.commit()

    return expired_count


async def expiry_loop():
    """Forever loop — run on app startup as a background task."""
    logger.info("Order expiry background task started")
    while True:
        try:
            count = await expire_stale_orders_once()
            if count > 0:
                logger.info(f"Expired {count} stale pending order(s)")
        except Exception as e:
            logger.exception(f"Order expiry task error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)