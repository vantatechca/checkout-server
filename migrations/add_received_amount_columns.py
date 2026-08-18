"""
One-shot migration — add `interac_payments.received_amount` and
`crypto_invoices.received_fiat`.

Why: both track the actual amount received vs. the expected amount (for
underpaid/overpaid detection), but these tables had drifted behind their
models — same class of gap as sync_orders_table.py and
add_original_price_to_order_items.py.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_received_amount_columns

Idempotent — re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


STATEMENTS = [
    ("interac_payments.received_amount",
     "ALTER TABLE interac_payments ADD COLUMN received_amount DECIMAL(10,2) NULL"),
    ("crypto_invoices.received_fiat",
     "ALTER TABLE crypto_invoices ADD COLUMN received_fiat DECIMAL(10,2) NULL"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        for name, sql in STATEMENTS:
            try:
                await conn.execute(text(sql))
                print(f"[OK] Added column {name}")
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    print(f"[skip] Column {name} already exists")
                else:
                    raise


if __name__ == "__main__":
    asyncio.run(run())
