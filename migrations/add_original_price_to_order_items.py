"""
One-shot migration — add `original_price` column to `order_items`.

Why: models/order.py's OrderItem carries both `price` (post-promo unit
price) and `original_price` (pre-discount unit price), but this table had
drifted behind the model — same class of gap as sync_orders_table.py.
Without it, every checkout insert (INSERT INTO order_items ... original_price
...) fails outright.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_original_price_to_order_items

Idempotent — re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


COLUMN_SQL = "ALTER TABLE order_items ADD COLUMN original_price DECIMAL(10,2) NULL"


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        try:
            await conn.execute(text(COLUMN_SQL))
            print("[OK] Added column order_items.original_price")
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                print("[skip] Column order_items.original_price already exists")
            else:
                raise


if __name__ == "__main__":
    asyncio.run(run())
