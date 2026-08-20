"""
One-shot migration — widen orders.tracking_url and orders.label_url from
VARCHAR(255) to TEXT.

Why: Shippo's real label_url values are signed URLs that routinely exceed
255 characters, and the buy-label endpoints crashed with a MySQL
"Data too long for column 'label_url'" error the moment a real label was
purchased — AFTER Shippo had already charged/created it, so the order was
left paid with no tracking info saved. tracking_url is widened too as a
precaution (carrier-dependent length).

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.widen_shippo_url_columns

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

COLUMNS = [
    ("tracking_url", "TEXT", "ALTER TABLE orders MODIFY COLUMN tracking_url TEXT NULL"),
    ("label_url",    "TEXT", "ALTER TABLE orders MODIFY COLUMN label_url TEXT NULL"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        existing = await conn.execute(text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
        ))
        current_types = {row[0]: row[1] for row in existing.fetchall()}

        for name, target_type, sql in COLUMNS:
            if current_types.get(name, "").lower() == target_type.lower():
                print(f"[SKIP] {name} is already {target_type}")
                continue
            await conn.execute(text(sql))
            print(f"[OK] Widened {name} to {target_type}")


if __name__ == "__main__":
    asyncio.run(run())
