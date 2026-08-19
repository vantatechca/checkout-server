"""
One-shot migration — add orders.paid_via_shippo.

Why: the admin dashboard's Shipping tab needs to show only orders that were
marked paid AND had their label bought through the Shippo flow (as opposed
to any paid order that happens to have a tracking number — a Shopify-paid
order can also get a label bought for it separately, and that shouldn't
count as a "Shipping tab" order). `shipped_at IS NOT NULL` alone can't tell
those apart, so this flag is set explicitly by the combined
mark-paid-and-buy-label endpoint and nothing else.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_paid_via_shippo_column

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("paid_via_shippo", "ALTER TABLE orders ADD COLUMN paid_via_shippo TINYINT(1) NOT NULL DEFAULT 0"),
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
