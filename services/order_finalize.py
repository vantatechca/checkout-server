"""
Shared "order just got marked paid" side-effect pipeline: create the Shopify
order, fire the affiliate webhook, and (optionally) send the customer
confirmation email.

Why this exists: this exact sequence used to be copy-pasted independently at
every place an order can transition to paid, and had quietly drifted —
Interac/Zelle manual match never fired the affiliate webhook at all, and five
webhook handlers (BTCPay, NowPayments, pymtz, WPay HPP, Whop) only fired it
when Shopify order creation happened to succeed, silently dropping affiliate
credit whenever Shopify was down/erroring on an otherwise-legitimate paid
order. Policy (explicit, from the business owner): the affiliate webhook
fires on every paid order, automatic or manual, regardless of Shopify's
outcome — so `_send_affiliate_webhook` below is unconditional and there is no
parameter to opt out of it. Consolidating here means every path to "paid"
enforces that same invariant instead of re-implementing it (correctly or not).
"""
import logging
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


class FinalizeResult(TypedDict):
    shopify_order_number: Optional[str]
    shopify_error:        Optional[str]


async def finalize_paid_order(
    order,
    db,
    *,
    send_email: bool = True,
    create_shopify: bool = True,
    label: str = "",
) -> FinalizeResult:
    """
    order: an Order ORM instance with `.items` already eager-loaded
           (selectinload(Order.items)) — required for both Shopify and email.
    db:    the AsyncSession used to look up the order's Brand for email accent.
    send_email: some callers intentionally skip the email here because their
           flow doesn't send one on this path (e.g. WPay HPP never has —
           preserved as-is, not changed by this consolidation).
    create_shopify: the admin dashboard's "Shipping" tab has a lightweight
           mark-paid path that deliberately does NOT create a Shopify order
           (fulfillment there is tracked entirely in this system) — every
           other caller leaves this at the default True. The affiliate
           webhook below is unaffected either way; it's unconditional
           regardless of this flag, per the policy explained above.
    label: short tag for log lines, e.g. "stripe_direct", "admin-mark-paid".

    Returns {"shopify_order_number": str|None, "shopify_error": str|None}.
    A real Shopify failure (bad/expired API key, Shopify rejecting the
    request, network error — as opposed to the store simply not having
    Shopify configured) is both logged AND appended to order.payment_notes,
    so it's visible on the order record itself, not just server logs — and
    callers with a human directly watching (the admin mark-paid endpoints)
    surface `shopify_error` in their response too. Never raises — every
    side effect is independently try/except'd so a failure in one doesn't
    block the others.
    """
    shopify_order_number = None
    shopify_error = None
    if create_shopify:
        from services.shopify import create_shopify_order, ShopifyOrderError
        try:
            shopify_order = await create_shopify_order(order)
            if shopify_order:
                shopify_order_number = str(shopify_order.get("order_number", ""))
                logger.info(f"✅ Shopify order #{shopify_order_number} created for {order.id} ({label})")
        except ShopifyOrderError as e:
            shopify_error = str(e)
            logger.error(f"Shopify order creation failed for {order.id} ({label}): {e}")
            try:
                order.payment_notes = (
                    (order.payment_notes or "") +
                    f" | ⚠ Shopify order creation failed: {shopify_error}"
                )[:1000]
                await db.commit()
            except Exception as note_err:
                logger.error(f"Could not persist Shopify failure note for {order.id}: {note_err}")
        except Exception as e:
            # Belt-and-suspenders — keeps the "never raises" guarantee even
            # for something outside the ShopifyOrderError contract.
            shopify_error = str(e)
            logger.error(f"Shopify order creation error for {order.id} ({label}): {e}")
    else:
        logger.info(f"Shopify order creation skipped for {order.id} ({label}) — create_shopify=False")

    # Unconditional — fires regardless of Shopify's outcome above. See module
    # docstring: this is a deliberate policy decision, not an oversight.
    try:
        from routes.webhooks import _send_affiliate_webhook
        await _send_affiliate_webhook(order)
    except Exception as e:
        logger.error(f"Affiliate webhook failed for {order.id} ({label}): {e}")

    if send_email and order.email:
        try:
            from models.brand import Brand
            from sqlalchemy import select
            brand_res = await db.execute(select(Brand).where(Brand.id == order.brand_id))
            brand = brand_res.scalar_one_or_none()
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            from services.email import send_confirmation_email
            result = await send_confirmation_email(order, shopify_order_number=shopify_order_number, accent=accent)
            if result:
                logger.info(f"✉️  Confirmation email sent for {order.id} ({label}) → {order.email}")
                try:
                    from models.order import CustomerEmailLog
                    db.add(CustomerEmailLog(
                        order_id   = order.id,
                        email_type = "confirmation",
                        sent_to    = order.email,
                        subject    = result["subject"],
                        body_text  = result.get("text"),
                        body_html  = result["html"],
                        sent_by    = "system",
                        success    = 1 if result["success"] else 0,
                    ))
                    await db.commit()
                except Exception as e:
                    logger.error(f"CustomerEmailLog write failed for {order.id} ({label}): {e}")
        except Exception as e:
            logger.error(f"Confirmation email failed for {order.id} ({label}): {e}")

    return {"shopify_order_number": shopify_order_number, "shopify_error": shopify_error}
