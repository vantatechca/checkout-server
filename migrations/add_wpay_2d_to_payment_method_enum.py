"""
One-shot migration — widen orders.payment_method ENUM to include 'wpay_2d'.

Why: models/order.py's PaymentMethod enum gained a `wpay_2d` member (WPay's
2D flow, routed through the WordPress/WooCommerce plugin site), but the
database column's ENUM type doesn't know about it until this runs — same
class of gap sync_orders_table.py fixed for 'wpay'/'zelle'/'altcoin' earlier.
Without this, any wpay_2d order insert/update fails outright.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_wpay_2d_to_payment_method_enum

Idempotent — MODIFY COLUMN re-applying the same/superset definition is a
no-op, no "already exists" error to catch.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


ALTER_SQL = (
    "ALTER TABLE orders MODIFY COLUMN payment_method "
    "ENUM('card','interac','crypto','zelle','altcoin','wpay','wpay_2d') NOT NULL"
)


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        await conn.execute(text(ALTER_SQL))
        print("[OK] Widened orders.payment_method ENUM to include wpay_2d")


if __name__ == "__main__":
    asyncio.run(run())
