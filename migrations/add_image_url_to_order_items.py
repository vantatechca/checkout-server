"""
One-shot migration — add `image_url` column to `order_items`.

Why: the v2 confirmation pages render the order summary with actual product
thumbnails (instead of a generic flask SVG). The image URL travels from the
Shopify cart through the checkout payload and gets stored here per line item.
Older orders without an image_url fall back to the SVG on render.

Usage on the VPS (and locally):
    cd /srv/shared/checkout-server
    python -m migrations.add_image_url_to_order_items

Idempotent — re-running is a no-op.
"""
import asyncio
from sqlalchemy import text
from database import engine as async_engine


COLUMN_SQL = "ALTER TABLE order_items ADD COLUMN image_url VARCHAR(500) NULL"


async def run() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async with async_engine.begin() as conn:
        try:
            await conn.execute(text(COLUMN_SQL))
            print("[OK] Added column order_items.image_url")
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                print("[skip] Column order_items.image_url already exists")
            else:
                raise


if __name__ == "__main__":
    asyncio.run(run())
