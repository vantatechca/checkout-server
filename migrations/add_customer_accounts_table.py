"""
Migration — add the `customer_accounts` table.

Stand-alone script. Run once on the VPS after deploy. Both invocations work:

    cd /srv/shared/checkout-server
    source venv/bin/activate
    python -m migrations.add_customer_accounts_table   # preferred
    # OR
    python migrations/add_customer_accounts_table.py    # also works

Idempotent: if the table already exists, this exits with a notice and does
nothing. Safe to re-run.
"""
import asyncio
import os
import sys

# Make `config`/`database`/etc importable regardless of how this script is
# invoked. When run as `python migrations/foo.py`, sys.path[0] is the
# migrations/ dir — not the project root — so `from config import settings`
# would fail. Prepending the project root fixes it for both `-m` and direct.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS customer_accounts (
    email          VARCHAR(255) NOT NULL,
    password_hash  VARCHAR(255) NOT NULL,
    first_name     VARCHAR(100) NULL,
    last_name      VARCHAR(100) NULL,
    phone          VARCHAR(50)  NULL,
    address1       VARCHAR(255) NULL,
    address2       VARCHAR(255) NULL,
    city           VARCHAR(100) NULL,
    province       VARCHAR(100) NULL,
    postal_code    VARCHAR(20)  NULL,
    country        VARCHAR(2)   NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (email),
    INDEX idx_customer_accounts_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def main() -> int:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        # Check if it already exists for nicer logging
        existed = await conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = :db AND table_name = 'customer_accounts'"
            ),
            {"db": settings.DB_NAME},
        )
        already = (existed.scalar() or 0) > 0

        await conn.execute(text(CREATE_SQL))

    await engine.dispose()

    if already:
        print("ℹ️  customer_accounts table already exists — nothing to do.")
    else:
        print("✅ customer_accounts table created.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
