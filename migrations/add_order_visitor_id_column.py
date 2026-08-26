"""
One-shot migration — add orders.visitor_id + a supporting index.

Why: the new visitor-tracking feature (models/visit.py, a cs_vid tracking
cookie set in main.py's checkout_page()) links an Order back to whichever
Visit row(s) share the same cookie value, so GET /admin/visits can show
each visit's outcome (paid/pending/none). The visits table itself is a
brand-new table with no VARCHAR-FK collation risk (see
migrations/create_missing_tables.py's docstring for why THAT table needed
hand-written DDL) — Base.metadata.create_all picks it up automatically on
next app startup. orders already exists in production, though, and
create_all never alters an existing table, so this column needs a real
migration.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_order_visitor_id_column

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        existing_columns = await conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
        ))
        existing_column_names = {row[0] for row in existing_columns.fetchall()}

        if "visitor_id" in existing_column_names:
            print("[SKIP] visitor_id already exists")
        else:
            await conn.execute(text("ALTER TABLE orders ADD COLUMN visitor_id VARCHAR(32) NULL"))
            print("[OK] Added column visitor_id")

        existing_indexes = await conn.execute(text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
        ))
        existing_index_names = {row[0] for row in existing_indexes.fetchall()}

        if "idx_orders_visitor_id" in existing_index_names:
            print("[SKIP] idx_orders_visitor_id already exists")
        else:
            await conn.execute(text("CREATE INDEX idx_orders_visitor_id ON orders (visitor_id)"))
            print("[OK] Created idx_orders_visitor_id")


if __name__ == "__main__":
    asyncio.run(run())
