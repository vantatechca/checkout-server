"""
One-shot migration — drop visits.city and visits.region.

Why: these were added minutes earlier for a MaxMind GeoLite2-based
design that required a signed-up license key. Switched instead to a
lazy, cached, ip-api.com-backed lookup (services/geoip.py) keyed by
ip_address in a new ip_geo_cache table — city/region no longer belong on
the Visit row itself, since the same IP is looked up once and reused
across every Visit that shares it, not resolved per-visit at write time.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.drop_visit_geo_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = ["city", "region"]


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

        for name in COLUMNS:
            if name not in existing_names:
                print(f"[SKIP] {name} already absent")
                continue
            await conn.execute(text(f"ALTER TABLE visits DROP COLUMN {name}"))
            print(f"[OK] Dropped column {name}")


if __name__ == "__main__":
    asyncio.run(run())
