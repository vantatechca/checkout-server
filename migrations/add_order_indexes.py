"""
One-shot migration — add indexes on orders.payment_ref and orders.payment_method.

Why: admin dashboard filters/queries (the `method=` filter, and the delayed-
card `payment_ref LIKE 'pay_%'`/'hr:%'/'wc:%' OR-clauses used across every
pending/awaiting/stats query in routes/admin.py) run on these columns with
no supporting index, forcing a full table scan that gets slower as order
volume grows. A plain B-tree index also accelerates `LIKE 'prefix%'` lookups
in MariaDB/MySQL, so this covers both usages.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_order_indexes

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

INDEXES = [
    ("idx_payment_ref",    "CREATE INDEX idx_payment_ref ON orders (payment_ref)"),
    ("idx_payment_method", "CREATE INDEX idx_payment_method ON orders (payment_method)"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        existing = await conn.execute(text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
        ))
        existing_names = {row[0] for row in existing.fetchall()}

        for name, sql in INDEXES:
            if name in existing_names:
                print(f"[SKIP] {name} already exists")
                continue
            await conn.execute(text(sql))
            print(f"[OK] Created {name}")


if __name__ == "__main__":
    asyncio.run(run())
