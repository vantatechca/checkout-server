"""
Migration — add `company` and `research_field` columns to the `orders` table.

Both columns are nullable — they're only collected on Princeton's checkout
(currently the only store using the Research Information section) and other
stores' orders will leave them NULL.

Usage on the VPS:

    cd /srv/shared/checkout-server
    source venv/bin/activate
    python migrations/add_company_research_columns.py

Idempotent — checks for column existence before adding. Safe to re-run.
"""
import asyncio
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings


async def _column_exists(conn, table: str, column: str) -> bool:
    q = text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = :db AND table_name = :t AND column_name = :c"
    )
    res = await conn.execute(q, {"db": settings.DB_NAME, "t": table, "c": column})
    return (res.scalar() or 0) > 0


async def main() -> int:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        added = []
        skipped = []

        if not await _column_exists(conn, "orders", "company"):
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN company VARCHAR(255) NULL"
            ))
            added.append("company")
        else:
            skipped.append("company")

        if not await _column_exists(conn, "orders", "research_field"):
            await conn.execute(text(
                "ALTER TABLE orders ADD COLUMN research_field VARCHAR(100) NULL"
            ))
            added.append("research_field")
        else:
            skipped.append("research_field")

    await engine.dispose()

    for c in added:
        print(f"✅ Added column orders.{c}")
    for c in skipped:
        print(f"ℹ️  Column orders.{c} already exists — skipped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
