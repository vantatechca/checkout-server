"""
One-shot migration — add visits.city and visits.region.

Why: the visits table itself was brand-new when it shipped (relied on
create_all, no migration needed) — but that's already happened on both
staging and production, so these two additional columns (added right
after, for MaxMind GeoLite2 city-level lookups in services/geoip.py) need
a real migration like any other column added to an existing table.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_visit_geo_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("city",   "ALTER TABLE visits ADD COLUMN city VARCHAR(100) NULL"),
    ("region", "ALTER TABLE visits ADD COLUMN region VARCHAR(100) NULL"),
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
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'visits'"
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
