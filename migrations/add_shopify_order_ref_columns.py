"""
One-shot migration — add orders.shopify_order_id and orders.shopify_order_number.

Why: previously, once finalize_paid_order() created a Shopify order for an
order, that link was never persisted anywhere — only used transiently for
the confirmation email and API response. That meant a later Shippo label
purchase for the same order had no way to also mark ITS Shopify order
fulfilled, so the two systems could silently drift out of sync (a real
tracking number in this system, but Shopify's own dashboard still showing
"unfulfilled" forever). This closes that gap going forward — see
services/order_finalize.py and the Shippo label-purchase endpoints in
routes/admin.py.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_shopify_order_ref_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("shopify_order_id",     "ALTER TABLE orders ADD COLUMN shopify_order_id VARCHAR(64) NULL"),
    ("shopify_order_number", "ALTER TABLE orders ADD COLUMN shopify_order_number VARCHAR(32) NULL"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        existing = await conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
        ))
        existing_names = {row[0] for row in existing.fetchall()}

        for name, sql in COLUMNS:
            if name in existing_names:
                print(f"[SKIP] {name} already exists")
                continue
            await conn.execute(text(sql))
            print(f"[OK] Added column {name}")


if __name__ == "__main__":
    asyncio.run(run())
