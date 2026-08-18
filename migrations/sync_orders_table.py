"""
One-shot migration — bring the `orders` table's schema up to date with
models/order.py. It had drifted significantly behind scripts/schema.sql:

  - Missing columns: original_subtotal, discount_code, promo_discount_pct,
    promo_discount_amount, last_customer_email_at, customer_emails_sent
  - payment_method ENUM only allowed ('card','interac','crypto') — missing
    'zelle', 'altcoin', 'wpay', so orders using any of those methods would
    fail to insert/update entirely.
  - payment_status ENUM was missing 'cancelled'.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.sync_orders_table

Idempotent — re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


ADD_COLUMNS = [
    ("original_subtotal",      "ALTER TABLE orders ADD COLUMN original_subtotal DECIMAL(10,2) NULL"),
    ("discount_code",          "ALTER TABLE orders ADD COLUMN discount_code VARCHAR(100) NULL"),
    ("promo_discount_pct",     "ALTER TABLE orders ADD COLUMN promo_discount_pct DECIMAL(5,2) DEFAULT 0"),
    ("promo_discount_amount",  "ALTER TABLE orders ADD COLUMN promo_discount_amount DECIMAL(10,2) DEFAULT 0"),
    ("last_customer_email_at", "ALTER TABLE orders ADD COLUMN last_customer_email_at DATETIME NULL"),
    ("customer_emails_sent",   "ALTER TABLE orders ADD COLUMN customer_emails_sent INT DEFAULT 0"),
]

# MODIFY COLUMN is naturally idempotent — re-applying the same/superset
# definition is a no-op, no "already exists" error to catch.
WIDEN_ENUMS = [
    ("payment_method ENUM",
     "ALTER TABLE orders MODIFY COLUMN payment_method "
     "ENUM('card','interac','crypto','zelle','altcoin','wpay') NOT NULL"),
    ("payment_status ENUM",
     "ALTER TABLE orders MODIFY COLUMN payment_status "
     "ENUM('pending','paid','failed','refunded','expired','manual','cancelled') "
     "DEFAULT 'pending'"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        for name, sql in ADD_COLUMNS:
            try:
                await conn.execute(text(sql))
                print(f"[OK] Added column orders.{name}")
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    print(f"[skip] Column orders.{name} already exists")
                else:
                    raise

        for name, sql in WIDEN_ENUMS:
            await conn.execute(text(sql))
            print(f"[OK] Widened orders.{name}")


if __name__ == "__main__":
    asyncio.run(run())
