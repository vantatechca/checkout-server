#!/usr/bin/env python3
"""
Convert the MySQL-dialect data dump (render_data_only.sql) into a
PostgreSQL-compatible import script for Neon (or any Postgres).

Fixes applied:
  - drop `SET FOREIGN_KEY_CHECKS=...;` lines (MySQL only)
  - strip backtick identifier quoting
  - INSERT IGNORE INTO  ->  INSERT INTO ... ON CONFLICT DO NOTHING
  - remap nowpayments_invoices columns to the real Postgres schema
        payment_id  -> np_invoice_id
        payment_id2 -> np_payment_id
        payment_url -> invoice_url
        expires_at  -> settled_at
  - wrap everything in one transaction
  - reset SERIAL sequences (brands, order_items, interac_payments,
    nowpayments_invoices) so future auto-inserts don't collide

Usage:
    python scripts/convert_dump_to_pg.py render_data_only.sql render_data_only.pg.sql
"""
import sys
import re

IN  = sys.argv[1] if len(sys.argv) > 1 else "render_data_only.sql"
OUT = sys.argv[2] if len(sys.argv) > 2 else "render_data_only.pg.sql"

# Old (MySQL dump) nowpayments column list -> correct Postgres column list.
NP_OLD = "nowpayments_invoices (id, order_id, payment_id, payment_id2, payment_url, coin, amount_fiat, received_fiat, status, expires_at, created_at)"
NP_NEW = "nowpayments_invoices (id, order_id, np_invoice_id, np_payment_id, invoice_url, coin, amount_fiat, received_fiat, status, settled_at, created_at)"

out_lines = []
out_lines.append("BEGIN;")

with open(IN, "r", encoding="utf-8") as f:
    for raw in f:
        line = raw.rstrip("\n")

        # Drop MySQL-only session toggles.
        if line.strip().startswith("SET FOREIGN_KEY_CHECKS"):
            continue
        if not line.strip():
            continue

        # Remove backtick identifier quoting (all identifiers here are
        # lowercase + unreserved, so they're safe unquoted in Postgres).
        line = line.replace("`", "")

        # MySQL "INSERT IGNORE" -> plain INSERT (idempotency handled below).
        line = line.replace("INSERT IGNORE INTO", "INSERT INTO")

        # Remap the nowpayments_invoices column list.
        line = line.replace(NP_OLD, NP_NEW)

        # Make each INSERT idempotent: turn the trailing ");" into
        # ") ON CONFLICT DO NOTHING;" so re-running is safe and existing
        # rows are skipped instead of erroring on the PK/unique constraints.
        if line.startswith("INSERT INTO") and line.endswith(");"):
            line = line[:-2] + ") ON CONFLICT DO NOTHING;"

        out_lines.append(line)

# Re-sync SERIAL sequences to MAX(id) so the next insert gets a fresh id.
# (orders.id is a VARCHAR ORD-xxxx, so it has no sequence.)
out_lines.append("")
for tbl in ("brands", "order_items", "interac_payments", "nowpayments_invoices"):
    out_lines.append(
        f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
        f"(SELECT COALESCE(MAX(id), 1) FROM {tbl}));"
    )

out_lines.append("COMMIT;")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines) + "\n")

print(f"Wrote {OUT} ({len(out_lines)} lines)")
