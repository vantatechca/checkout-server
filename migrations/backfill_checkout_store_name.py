"""
One-time data fix — replace the literal store_name "Checkout" with the
order's actual source_domain, wherever one is available.

Why: routes/checkout.py's _create_base_order() and checkout_reserve()
used to default store_name to the bare word "Checkout" whenever no Brand
row matched the request's domain — meaningless once it shows up in the
admin dashboard (Orders list, Visits tab "Started Checkout, Not
Completed" table), since it doesn't identify which store the order
actually came from. That code path is already fixed to fall back to the
real domain instead going forward (see that file's git history) — this
migration is the one-time backfill for orders created before that fix,
which still have the old placeholder permanently stored.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.backfill_checkout_store_name

Safely re-runnable — after the first run, no rows match the WHERE clause
anymore (aside from genuinely new orders where even source_domain was
unavailable, which the fixed code only produces in the rare case of a
missing Host header entirely).
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        result = await conn.execute(text(
            "UPDATE orders SET store_name = source_domain "
            "WHERE store_name = 'Checkout' "
            "AND source_domain IS NOT NULL AND source_domain != ''"
        ))
        print(f"[OK] Updated {result.rowcount} order(s) — store_name 'Checkout' -> actual source_domain")


if __name__ == "__main__":
    asyncio.run(run())
