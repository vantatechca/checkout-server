"""
One-shot migration — add orders.settled_currency and orders.settled_amount.

Why: pymtz, WPay HPP, and WPay 2D are USD-only processors — a CAD cart gets
converted and charged in USD, but `orders.total`/`orders.currency` stay the
CAD cart price on purpose (Shopify order creation and affiliate commission
payouts both read `total` directly; changing it would alter Shopify's own
accounting and reduce affiliate payouts for these orders). These two new
columns capture what was ACTUALLY charged, for admin-facing display only.
NULL means no conversion happened — `total`/`currency` already reflect the
real charge.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_settled_currency_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("settled_currency", "ALTER TABLE orders ADD COLUMN settled_currency VARCHAR(3) NULL"),
    ("settled_amount",   "ALTER TABLE orders ADD COLUMN settled_amount DECIMAL(10,2) NULL"),
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
