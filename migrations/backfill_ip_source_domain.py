"""
One-time data fix — null out source_domain wherever it's a raw IP
address (a bot/scanner hitting the server's IP directly instead of a
real domain, before routes/checkout.py's/main.py's _looks_like_ip fix
existed), on both orders and visits.

Why: source_domain used to fall back all the way to the Host header
with no check on what it actually was. When accessed by raw IP, the
Host header IS that IP, so it got stored as if it were a store domain —
surfacing in the admin dashboard's Visits tab "Top Referring Store"
ranking as a fake "store" that's really just an IP address. That
fallback chain is already fixed to stop this going forward; this is the
one-time backfill for rows written before the fix.

Efficient even against a large table: checks only the DISTINCT
source_domain values (a store's real domain repeats across thousands of
orders, but there are only a handful of distinct values to test), then
does one bulk UPDATE for whichever of those turned out to be IPs —
never a per-row Python loop.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.backfill_ip_source_domain

Safely re-runnable — after the first run, no IP-shaped values remain to
find (aside from any new ones written before a restart picks up the
fixed code, which is exactly what this script exists to keep cleaning
up if re-run).
"""
import asyncio
import ipaddress

from sqlalchemy import bindparam, text
from database import engine as async_engine


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        for table in ("orders", "visits"):
            result = await conn.execute(text(
                f"SELECT DISTINCT source_domain FROM {table} WHERE source_domain IS NOT NULL"
            ))
            distinct_values = [row[0] for row in result.fetchall()]
            bad_values = [v for v in distinct_values if v and _looks_like_ip(v)]

            if not bad_values:
                print(f"[OK] {table}: no IP-shaped source_domain values found")
                continue

            stmt = text(f"UPDATE {table} SET source_domain = NULL WHERE source_domain IN :bad_values").bindparams(
                bindparam("bad_values", expanding=True)
            )
            update_result = await conn.execute(stmt, {"bad_values": bad_values})
            print(f"[OK] {table}: nulled {update_result.rowcount} row(s) across {len(bad_values)} IP-shaped value(s): {bad_values}")


if __name__ == "__main__":
    asyncio.run(run())
