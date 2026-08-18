"""
One-shot migration — add a composite index on orders (payment_status, paid_at).

Why: "today's revenue," "paid today," and the /admin/monitoring/health KPI
queries all filter on `payment_status = 'paid' AND paid_at >= <today_start>`
together. The single-column indexes on payment_status/created_at don't cover
that combined filter efficiently; a composite index does.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_status_paidat_index

Idempotent — checks information_schema first so re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

INDEX_NAME = "idx_status_paidat"
INDEX_SQL = "CREATE INDEX idx_status_paidat ON orders (payment_status, paid_at)"


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

        if INDEX_NAME in existing_names:
            print(f"[SKIP] {INDEX_NAME} already exists")
        else:
            await conn.execute(text(INDEX_SQL))
            print(f"[OK] Created {INDEX_NAME}")


if __name__ == "__main__":
    asyncio.run(run())
