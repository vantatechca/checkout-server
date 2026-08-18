"""
One-shot migration — widen customer_email_log.email_type ENUM to include
'confirmation', and add supporting indexes for the new /admin/emails endpoint
(list-all-emails view, filterable by type, sorted by sent_at).

Why: CustomerEmailLog previously only logged admin-sent reminder/underpaid
emails. services/order_finalize.py now also logs the automatic post-payment
confirmation email (sent_by="system") through this same table, so every
customer-facing email is auditable in one place.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_confirmation_email_type

Idempotent — MODIFY COLUMN re-applying the same/superset ENUM is a no-op;
indexes are checked against information_schema before creating.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine

ALTER_SQL = (
    "ALTER TABLE customer_email_log MODIFY COLUMN email_type "
    "ENUM('reminder','underpaid','confirmation') NOT NULL"
)

INDEXES = [
    ("idx_email_type", "CREATE INDEX idx_email_type ON customer_email_log (email_type)"),
    ("idx_sent_at",    "CREATE INDEX idx_sent_at ON customer_email_log (sent_at)"),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        await conn.execute(text(ALTER_SQL))
        print("[OK] Widened customer_email_log.email_type ENUM to include 'confirmation'")

        existing = await conn.execute(text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'customer_email_log'"
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
