"""
One-shot migration — create tables that exist as SQLAlchemy models but were
never added to scripts/schema.sql: admin_activities, zelle_payments,
customer_email_log, nowpayments_invoices.

Why explicit CREATE TABLE instead of Base.metadata.create_all(): the server's
default collation is utf8mb4_uca1400_ai_ci (MariaDB 11 default), but
orders.id was created with utf8mb4_unicode_ci by schema.sql. Base.metadata
.create_all() doesn't pin a collation, so it inherits the server default —
which then mismatches orders.id on the zelle_payments/nowpayments_invoices
foreign keys and MariaDB rejects the FK with errno 150. These CREATE TABLE
statements pin utf8mb4_unicode_ci to match.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.create_missing_tables

Idempotent — re-running is a no-op (CREATE TABLE IF NOT EXISTS).
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


STATEMENTS = [
    ("admin_activities", """
        CREATE TABLE IF NOT EXISTS admin_activities (
            id BIGINT NOT NULL AUTO_INCREMENT,
            created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6) NOT NULL,
            admin_user VARCHAR(64),
            action VARCHAR(64) NOT NULL,
            target_type VARCHAR(32),
            target_id VARCHAR(64),
            details TEXT,
            ip_address VARCHAR(64),
            PRIMARY KEY (id),
            INDEX ix_admin_activities_created_at (created_at),
            INDEX ix_admin_activities_admin_user (admin_user),
            INDEX ix_admin_activities_action (action),
            INDEX ix_admin_activities_target_id (target_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """),
    ("zelle_payments", """
        CREATE TABLE IF NOT EXISTS zelle_payments (
            id INTEGER NOT NULL AUTO_INCREMENT,
            order_id VARCHAR(20),
            expected_amount NUMERIC(10, 2) NOT NULL,
            received_amount NUMERIC(10, 2),
            sender_name VARCHAR(255),
            sender_email VARCHAR(255),
            matched_at DATETIME,
            raw_email_id VARCHAR(255),
            status ENUM('waiting','matched','unmatched','manual','underpaid'),
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE (order_id),
            UNIQUE (raw_email_id),
            FOREIGN KEY (order_id) REFERENCES orders (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """),
    ("customer_email_log", """
        CREATE TABLE IF NOT EXISTS customer_email_log (
            id INTEGER NOT NULL AUTO_INCREMENT,
            order_id VARCHAR(20) NOT NULL,
            email_type ENUM('reminder','underpaid') NOT NULL,
            sent_to VARCHAR(255) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            body_text TEXT,
            body_html TEXT,
            sent_by VARCHAR(100) DEFAULT 'admin',
            success INTEGER DEFAULT 1,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            INDEX ix_customer_email_log_order_id (order_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """),
    ("nowpayments_invoices", """
        CREATE TABLE IF NOT EXISTS nowpayments_invoices (
            id INTEGER NOT NULL AUTO_INCREMENT,
            order_id VARCHAR(20),
            np_invoice_id VARCHAR(255) NOT NULL,
            np_payment_id VARCHAR(255),
            invoice_url TEXT,
            coin VARCHAR(50),
            amount_fiat NUMERIC(10, 2) NOT NULL,
            received_fiat NUMERIC(10, 2),
            status VARCHAR(50) DEFAULT 'waiting',
            settled_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE (order_id),
            UNIQUE (np_invoice_id),
            FOREIGN KEY (order_id) REFERENCES orders (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """),
]


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        for name, ddl in STATEMENTS:
            await conn.execute(text(ddl))
            print(f"[OK] Ensured table {name}")


if __name__ == "__main__":
    asyncio.run(run())
