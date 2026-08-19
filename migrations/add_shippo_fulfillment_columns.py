"""
One-shot migration — add Shippo shipping-label fulfillment columns to orders.

Why: admins need to buy a shipping label from the admin dashboard and have
the resulting tracking number/label surfaced back on the order. These
columns are all NULL until a label is actually purchased; `shipped_at`
doubles as the "has this order been shipped yet" flag.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server           # or checkout-server-staging
    python -m migrations.add_shippo_fulfillment_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("tracking_number",       "ALTER TABLE orders ADD COLUMN tracking_number VARCHAR(64) NULL"),
    ("tracking_url",          "ALTER TABLE orders ADD COLUMN tracking_url VARCHAR(255) NULL"),
    ("carrier",                "ALTER TABLE orders ADD COLUMN carrier VARCHAR(32) NULL"),
    ("label_url",              "ALTER TABLE orders ADD COLUMN label_url VARCHAR(255) NULL"),
    ("shippo_transaction_id",  "ALTER TABLE orders ADD COLUMN shippo_transaction_id VARCHAR(64) NULL"),
    ("shipped_at",             "ALTER TABLE orders ADD COLUMN shipped_at DATETIME NULL"),
    ("package_weight_oz",      "ALTER TABLE orders ADD COLUMN package_weight_oz DECIMAL(6,2) NULL"),
    ("package_length_in",      "ALTER TABLE orders ADD COLUMN package_length_in DECIMAL(5,2) NULL"),
    ("package_width_in",       "ALTER TABLE orders ADD COLUMN package_width_in DECIMAL(5,2) NULL"),
    ("package_height_in",      "ALTER TABLE orders ADD COLUMN package_height_in DECIMAL(5,2) NULL"),
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
