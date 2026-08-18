"""
One-shot data backfill — populate settled_currency/settled_amount for
historical CAD orders on the three USD-only processors (pymtz, WPay HPP,
WPay 2D) that converted before this tracking existed (see
add_settled_currency_columns.py and models/order.py).

Scope, deliberately narrow:
  - wpay / wpay_2d: EVERY CAD-currency order on these methods went through
    the CAD->USD conversion (it's the only path those two support), so all
    of them qualify.
  - card: only the subset pymtz actually processed — identified the same
    way models.order._classify_processor() already does, via payment_ref
    starting with "pay_". Helcim/Stripe/Auth.net also use PaymentMethod.card
    but charge natively with no conversion, so they're deliberately excluded
    — backfilling them would incorrectly invent a "charged amount" that
    never happened.

Uses the same USD_CONVERSION_RATE the app used at charge time (1.38,
unchanged since these orders were created). If that rate is ever adjusted
going forward, do not re-run this against old orders using the new rate —
their real historical charge used whatever rate was in effect then.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.backfill_settled_currency          # dry run (default)
    python -m migrations.backfill_settled_currency --apply   # actually writes

Idempotent — only touches rows where settled_currency IS NULL, so re-running
after a partial apply just picks up what's left.
"""
import asyncio
import sys
from sqlalchemy import select
from database import AsyncSessionLocal
from models.order import Order, PaymentMethod

USD_CONVERSION_RATE = 1.38


async def run(apply: bool) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with AsyncSessionLocal() as db:
        wpay_result = await db.execute(
            select(Order).where(
                Order.payment_method.in_([PaymentMethod.wpay, PaymentMethod.wpay_2d]),
                Order.currency == "CAD",
                Order.settled_currency.is_(None),
            )
        )
        wpay_orders = wpay_result.scalars().all()

        pymtz_result = await db.execute(
            select(Order).where(
                Order.payment_method == PaymentMethod.card,
                Order.currency == "CAD",
                Order.payment_ref.like("pay_%"),
                Order.settled_currency.is_(None),
            )
        )
        pymtz_orders = pymtz_result.scalars().all()

        all_orders = list(wpay_orders) + list(pymtz_orders)
        print(f"Found {len(wpay_orders)} wpay/wpay_2d CAD orders and "
              f"{len(pymtz_orders)} pymtz CAD orders to backfill "
              f"({len(all_orders)} total).")

        if not apply:
            print("[DRY RUN] No changes written. Re-run with --apply to commit.")
            for o in all_orders[:10]:
                usd = round(float(o.total) / USD_CONVERSION_RATE, 2)
                print(f"  would set {o.id}: settled_currency=USD, settled_amount={usd} (from total={o.total} CAD)")
            if len(all_orders) > 10:
                print(f"  ... and {len(all_orders) - 10} more")
            return

        for o in all_orders:
            o.settled_currency = "USD"
            o.settled_amount = round(float(o.total) / USD_CONVERSION_RATE, 2)

        await db.commit()
        print(f"[OK] Backfilled {len(all_orders)} orders.")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
