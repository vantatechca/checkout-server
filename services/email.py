"""
Outbound transactional email via Resend.
Local dev: sends from onboarding@resend.dev (Resend's test sender).
Production: sends from support@order-confirmed.com.
"""
import html as html_lib
import logging
import re
import httpx
from typing import Optional, Dict, Any

from config import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"

# Verified domain in Resend — works in both dev and production
FROM_ADDRESS = "Peps Checkout Support <support@pepscustomercare.com>"
REPLY_TO = "support@pepscustomercare.com"


# ─── Core sender ─────────────────────────────────────────────────────────────

async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    reply_to: str = REPLY_TO,
) -> bool:
    """Returns True on success. Never raises."""
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return False

    payload: Dict[str, Any] = {
        "from":     FROM_ADDRESS,
        "to":       [to],
        "subject":  subject,
        "html":     html,
        "reply_to": reply_to,
        "headers": {
            "List-Unsubscribe": f"<mailto:{REPLY_TO}?subject=unsubscribe>",
            "X-Entity-Ref-ID":  "checkout-customer-notice",
        },
    }
    if text:
        payload["text"] = text

    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type":  "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(RESEND_URL, json=payload, headers=headers)
        if r.status_code in (200, 202):
            logger.info("Email OK -> %s | %s", to, subject)
            return True
        logger.error("Resend %s -> %s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.error("Resend exception: %s", e)
        return False


# ─── HTML wrapper ────────────────────────────────────────────────────────────

def _wrap_html(body_html: str) -> str:
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#222;padding:24px;line-height:1.5">
      {body_html}
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
      <p style="font-size:12px;color:#999;margin:0">
        This message was sent in relation to your order. If you have any questions, please use the chat support available on our website.
      </p>
    </div>
    """


# ─── Template: payment reminder (unified — handles $0 and partial payments) ─

def build_payment_reminder_template(
    order, received_amount: float, payment_email: str, accent: str = "#dd1d1d"
) -> Dict[str, str]:
    """
    Single reminder template covering both scenarios:
      - received_amount == 0  → "We haven't received your payment yet"
      - received_amount  > 0  → "Payment received but short — please send remaining $X"
    Now includes:
      - The store domain (e.g., eaststpaulpeps.com) where the order was placed
      - A full order summary table (items, qty, price, line total)
    """
    pm_value = order.payment_method.value if hasattr(
        order.payment_method, "value") else str(order.payment_method)
    method_lbl = "Interac e-Transfer" if pm_value == "interac" else "Zelle"
    contact_lbl = "email address" if pm_value == "interac" else "Zelle contact"

    # Display the ORIGINAL subtotal (pre-promo) when available, else fall back to stored subtotal
    post_promo_subtotal = float(order.subtotal or 0)
    original_subtotal = float(
        getattr(order, "original_subtotal", None) or post_promo_subtotal)
    subtotal = original_subtotal

    total = float(order.total)
    received = float(received_amount or 0)
    remaining = round(total - received, 2)
    currency = order.currency or "CAD"
    is_partial = received > 0

    # ─── Promo code discount (e.g. HANNAH10 -10%) ────────────────────────────
    promo_amount = float(getattr(order, "promo_discount_amount", 0) or 0)
    promo_pct = float(getattr(order, "promo_discount_pct", 0) or 0)
    discount_code = order.discount_code or ""

    # ─── Payment-method discount (Interac/Zelle 5%, Crypto 10%) ──────────────
    method_amount = float(order.discount_amount or 0)
    method_pct = float(order.discount_pct or 0)

    # Build separate rows for each active discount layer
    discount_row_html = ""

    if promo_amount > 0:
        code_label = f' (<strong>{html_lib.escape(discount_code)}</strong>)' if discount_code else ""
        p_pct_label = f" &mdash; {promo_pct:.0f}%" if promo_pct > 0 else ""
        discount_row_html += (
            f'<tr><td style="padding:6px 0;color:#1a7e2e">Promo discount{code_label}{p_pct_label}</td>'
            f'<td style="padding:6px 0;text-align:right;color:#1a7e2e">-${promo_amount:.2f} {currency}</td></tr>'
        )

    if method_amount > 0:
        if pm_value == "interac":
            method_label = "Interac e-Transfer discount"
        elif pm_value == "zelle":
            method_label = "Zelle discount"
        elif pm_value == "crypto":
            method_label = "Crypto discount"
        else:
            method_label = "Discount"
        m_pct_label = f" &mdash; {method_pct:.0f}%" if method_pct > 0 else ""
        discount_row_html += (
            f'<tr><td style="padding:6px 0;color:#1a7e2e">{method_label}{m_pct_label}</td>'
            f'<td style="padding:6px 0;text-align:right;color:#1a7e2e">-${method_amount:.2f} {currency}</td></tr>'
        )

    # Store identification — prefer source_domain, fallback to store_name
    store_domain = (order.source_domain or "").replace(
        "www.", "") if order.source_domain else ""
    store_label = store_domain or order.store_name or "our store"

    # ─── Build line-items section ────────────────────────────────────────
    items = list(order.items) if order.items else []

    items_html = ""
    items_text_lines = []
    if items:
        rows_html = ""
        # If a promo was applied, scale stored item prices BACK UP to original
        promo_factor = 1.0
        if promo_amount > 0 and post_promo_subtotal > 0:
            promo_factor = original_subtotal / post_promo_subtotal

        for it in items:
            qty = int(it.qty or 1)
            stored_price = float(it.price or 0)
            # Prefer original_price if stored on the item, else scale from promo factor
            stored_orig = getattr(it, "original_price", None)
            if stored_orig:
                price = float(stored_orig)
            else:
                price = round(stored_price * promo_factor, 2)
            line = round(price * qty, 2)
            title_raw = it.title or ""
            title = html_lib.escape(title_raw)
            variant = f' <span style="color:#999">({html_lib.escape(it.variant)})</span>' if it.variant else ""

            rows_html += (
                f'<tr>'
                f'<td style="padding:8px 0;border-bottom:1px solid #eee">{title}{variant}</td>'
                f'<td style="padding:8px 6px;text-align:center;border-bottom:1px solid #eee;color:#666">{qty}</td>'
                f'<td style="padding:8px 0;text-align:right;border-bottom:1px solid #eee">${price:.2f}</td>'
                f'<td style="padding:8px 0 8px 8px;text-align:right;border-bottom:1px solid #eee;font-weight:600">${line:.2f}</td>'
                f'</tr>'
            )

            variant_text = f" ({it.variant})" if it.variant else ""
            items_text_lines.append(
                f"  - {title_raw}{variant_text}  x{qty}  ${line:.2f}"
            )

        items_html = f"""
      <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.06em;color:#888;margin:24px 0 8px">Order Summary</h3>
      <table style="border-collapse:collapse;width:100%;font-size:14px;margin:0 0 16px">
        <thead>
          <tr style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:0.04em">
            <th style="text-align:left;padding:6px 0;border-bottom:1px solid #ddd;font-weight:500">Product</th>
            <th style="text-align:center;padding:6px 0;border-bottom:1px solid #ddd;font-weight:500">Qty</th>
            <th style="text-align:right;padding:6px 0;border-bottom:1px solid #ddd;font-weight:500">Price</th>
            <th style="text-align:right;padding:6px 0;border-bottom:1px solid #ddd;font-weight:500">Total</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
        """

    if is_partial:
        subject = f"Action needed: ${remaining:.2f} {currency} remaining on order {order.id}"
        heading = "Payment received &mdash; but short"
        intro_paragraph = (
            f"<p>We received your {method_lbl}, but the amount was less than the order total. "
            f"To finish processing your order, please send the remaining balance to the same "
            f"{contact_lbl}: <strong>{payment_email}</strong>.</p>"
        )
    else:
        subject = f"Reminder: ${total:.2f} {currency} payment pending for order {order.id}"
        heading = "Friendly reminder about your order"
        intro_paragraph = (
            f"<p>We're holding your order, but we haven't yet received your {method_lbl} payment. "
            f"To complete your purchase, please send the full balance to our "
            f"{contact_lbl}: <strong>{payment_email}</strong>.</p>"
        )

    # Store label for the header (shown as a clickable link if it's a domain).
    # source_domain is client-submitted (checkout ?source= param, no
    # server-side whitelist) and lands in both an href attribute and link
    # text here — escaped for both contexts.
    if store_domain:
        _store_domain_safe = html_lib.escape(store_domain)
        store_html = f'<a href="https://{_store_domain_safe}" style="color:{accent};text-decoration:none">{_store_domain_safe}</a>'
    else:
        store_html = html_lib.escape(store_label)

    body_html = f"""
      <h2 style="color:{accent};margin:0 0 4px">{heading}</h2>
      <p style="color:#666;margin:0 0 16px;font-size:13px">
        Order <strong style="color:#222">{order.id}</strong> &middot; placed at {store_html}
      </p>

      <p>Hi {html_lib.escape(order.first_name) if order.first_name else 'there'},</p>

      {intro_paragraph}

      {items_html}

      <table style="border-collapse:collapse;margin:8px 0 16px;width:100%;font-size:14px">
        <tr><td style="padding:6px 0;color:#666">Subtotal</td>
            <td style="padding:6px 0;text-align:right">${subtotal:.2f} {currency}</td></tr>
        {discount_row_html}
        <tr><td style="padding:6px 0;color:#666;border-top:1px solid #eee">Order total</td>
            <td style="padding:6px 0;text-align:right;border-top:1px solid #eee">${total:.2f} {currency}</td></tr>
        <tr><td style="padding:6px 0;color:#666">Amount received</td>
            <td style="padding:6px 0;text-align:right">${received:.2f} {currency}</td></tr>
        <tr style="border-top:2px solid {accent}">
            <td style="padding:10px 0;font-weight:bold">Remaining balance</td>
            <td style="padding:10px 0;text-align:right;font-weight:bold;color:{accent}">${remaining:.2f} {currency}</td></tr>
      </table>

      <div style="background:#f7f7f7;padding:12px;border-radius:4px;font-size:14px;margin:16px 0">
        <strong>Send to:</strong> {payment_email}<br>
        <strong>Amount:</strong> ${remaining:.2f} {currency}<br>
        <strong>Memo / message:</strong> {order.id}
      </div>

      <p style="font-size:14px;color:#555">
        Please use the same name and email as the one on your order so we can match the payment automatically.
        Your order will ship as soon as the full balance is received.
      </p>
    """

    # Plain-text version (for email clients that don't render HTML)
    items_text = ""
    if items_text_lines:
        items_text = "Order summary:\n" + "\n".join(items_text_lines) + "\n\n"

    # Plain-text discount lines (one per active layer)
    discount_text = ""
    if promo_amount > 0:
        code_part = f" ({discount_code})" if discount_code else ""
        p_pct_part = f" - {promo_pct:.0f}%" if promo_pct > 0 else ""
        discount_text += f"Promo discount{code_part}{p_pct_part}: -${promo_amount:.2f} {currency}\n"
    if method_amount > 0:
        if pm_value == "interac":
            mtxt = "Interac e-Transfer discount"
        elif pm_value == "zelle":
            mtxt = "Zelle discount"
        elif pm_value == "crypto":
            mtxt = "Crypto discount"
        else:
            mtxt = "Discount"
        m_pct_part = f" - {method_pct:.0f}%" if method_pct > 0 else ""
        discount_text += f"{mtxt}{m_pct_part}: -${method_amount:.2f} {currency}\n"

    text = (
        f"Hi {order.first_name or 'there'},\n\n"
        f"{('We received your ' + method_lbl + ' for order ' + order.id + ', but the amount was short.') if is_partial else ('We are holding your order ' + order.id + ' but have not yet received your ' + method_lbl + ' payment.')}\n\n"
        f"Store:    {store_label}\n"
        f"Order:    {order.id}\n\n"
        f"{items_text}"
        f"Subtotal:        ${subtotal:.2f} {currency}\n"
        f"{discount_text}"
        f"Order total:     ${total:.2f} {currency}\n"
        f"Amount received: ${received:.2f} {currency}\n"
        f"Remaining:       ${remaining:.2f} {currency}\n\n"
        f"Send to: {payment_email}\n"
        f"Amount:  ${remaining:.2f} {currency}\n"
        f"Memo:    {order.id}\n\n"
        f"Use the same name/email as on your order. You can contact us via chat support on our website if you need assistance OR IF YOU HAVE ANY QUESTIONS\n"
    )

    return {"subject": subject, "html": _wrap_html(body_html), "text": text}


# ─── Backwards-compat aliases (so existing imports don't break) ─────────────

def build_reminder_template(order, payment_email: str, accent: str = "#dd1d1d"):
    """Legacy: $0-paid reminder. Routes to the unified template with received=0."""
    return build_payment_reminder_template(order, 0, payment_email, accent)


def build_underpaid_template(order, received_amount: float, payment_email: str, accent: str = "#dd1d1d"):
    """Legacy: partial-payment reminder. Routes to the unified template."""
    return build_payment_reminder_template(order, received_amount, payment_email, accent)


# ─── Template: order received (sent the moment a manual-transfer order is
#     placed — Interac or Zelle) ────────────────────────────────────────────
# Reassures the customer their order actually went through (they just sent a
# manual e-Transfer/Zelle payment — there's no instant "charged!" moment like
# a card does), and gives them a way to reach support if the follow-up
# confirmation never arrives. Sent immediately by checkout_interac()/
# checkout_zelle() in routes/checkout.py — separate from
# build_confirmation_template, which only fires once the payment is actually
# matched/marked paid. Zelle points the customer at the support inbox
# (ORDER_SUPPORT_EMAIL); every other method points them at the website's
# chat widget instead — same pattern the rest of these templates already use
# ("Questions? Use the chat widget on our website").

def build_order_received_template(order, accent: str = "#dd1d1d") -> Dict[str, str]:
    pm_value = order.payment_method.value if hasattr(
        order.payment_method, "value") else str(order.payment_method)
    # ORDER_SUPPORT_EMAIL is a US-only inbox — gate on currency (not just
    # payment method) so it can never surface for a CAD order even in an
    # edge case where one somehow carries payment_method="zelle" (Zelle is
    # meant to be US-only, but currency is the more direct, harder
    # guarantee than trusting that invariant holds everywhere upstream).
    is_zelle = pm_value == "zelle" and (order.currency or "").upper() != "CAD"
    support_email = getattr(settings, "ORDER_SUPPORT_EMAIL", "") or REPLY_TO
    follow_up_html = (
        f'please write to <a href="mailto:{support_email}" style="color:{accent};text-decoration:none">{support_email}</a>.'
        if is_zelle else
        "please use the chat widget on our website."
    )
    follow_up_text = f"please write to {support_email}." if is_zelle else "please use the chat widget on our website."
    subject = f"Order received — {order.id}"
    body_html = f"""
      <p>Hi {html_lib.escape(order.first_name) if order.first_name else 'there'},</p>
      <p>Your order <strong>{order.id}</strong> has been received. You will receive a payment
      confirmation email within 24–48 hours, depending on the number of orders we are currently processing.</p>
      <p>If you haven't received a confirmation email after that time, {follow_up_html}</p>
    """
    text = (
        f"Hi {order.first_name or 'there'},\n\n"
        f"Your order {order.id} has been received. You will receive a payment "
        f"confirmation email within 24–48 hours, depending on the number of orders we are currently processing.\n\n"
        f"If you haven't received a confirmation email after that time, {follow_up_text}\n"
    )
    return {"subject": subject, "html": _wrap_html(body_html), "text": text}


# ─── Helper: convert plain text to safe HTML ─────────────────────────────────

def text_to_html(text: str) -> str:
    escaped = (text.replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
    paragraphs = [f"<p>{p.strip().replace(chr(10), '<br>')}</p>"
                  for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    return _wrap_html("\n".join(paragraphs))

# ─── Template: order confirmation (sent when an order is marked paid) ────────


def build_confirmation_template(
    order,
    shopify_order_number: Optional[str] = None,
    accent: str = "#dd1d1d",
) -> Dict[str, str]:
    pm_value = order.payment_method.value if hasattr(
        order.payment_method, "value") else str(order.payment_method)
    method_labels = {
        "interac": "Interac e-Transfer",
        "zelle":   "Zelle",
        "card":    "Credit Card",
        "wpay":    "Card",
        "wpay_2d": "Card",
        "crypto":  "Cryptocurrency (BTC/LN)",
        "altcoin": "Cryptocurrency (Altcoin)",
    }
    method_lbl = method_labels.get(pm_value, pm_value.title())
    currency = order.currency or "CAD"
    total = float(order.total)
    promo_amount = float(getattr(order, "promo_discount_amount", 0) or 0)
    promo_pct = float(getattr(order, "promo_discount_pct", 0) or 0)
    discount_code = order.discount_code or ""
    method_amount = float(order.discount_amount or 0)
    method_pct = float(order.discount_pct or 0)
    post_promo_subtotal = float(order.subtotal or 0)
    original_subtotal = float(
        getattr(order, "original_subtotal", None) or post_promo_subtotal)
    subtotal = original_subtotal
    # WPay (both the HPP flow and WPay 2D via WooCommerce) settles in USD —
    # CAD orders get converted server-side at charge time (see
    # USD_CONVERSION_RATE in routes/checkout.py), and the actual USD amount
    # charged is stored on order.settled_amount/settled_currency. Show that
    # alongside the CAD total the customer saw at checkout so the two numbers
    # aren't mistaken for a discrepancy.
    settled_amount = float(order.settled_amount) if getattr(
        order, "settled_amount", None) is not None else None
    settled_currency = getattr(order, "settled_currency", None)
    show_usd_charge = (
        pm_value in ("wpay", "wpay_2d")
        and settled_currency
        and settled_amount is not None
        and settled_currency != currency
    )
    store_domain = (order.source_domain or "").replace(
        "www.", "") if order.source_domain else ""
    store_label = store_domain or order.store_name or "our store"
    # source_domain is client-submitted (checkout ?source= param, no
    # server-side whitelist) and lands in both an href attribute and link
    # text here — escaped for both contexts, same as the reminder template above.
    store_html = (
        f'<a href="https://{html_lib.escape(store_domain)}" style="color:{accent};text-decoration:none">{html_lib.escape(store_domain)}</a>'
        if store_domain else html_lib.escape(store_label)
    )
    ref_label = f"#{shopify_order_number}" if shopify_order_number else order.id
    subject = f"Order confirmed — {ref_label} | {store_label}"

    items = list(order.items) if order.items else []
    rows_html = ""
    items_text_lines = []
    promo_factor = (original_subtotal / post_promo_subtotal) if (
        promo_amount > 0 and post_promo_subtotal > 0) else 1.0

    for it in items:
        qty = int(it.qty or 1)
        stored_orig = getattr(it, "original_price", None)
        price = float(stored_orig) if stored_orig else round(
            float(it.price or 0) * promo_factor, 2)
        line = round(price * qty, 2)
        title = html_lib.escape(it.title or "")
        variant_html = f' <span style="color:#999;font-size:13px">({html_lib.escape(it.variant)})</span>' if it.variant else ""
        variant_text = f" ({it.variant})" if it.variant else ""
        rows_html += (
            f'<tr>'
            f'<td style="padding:10px 0;border-bottom:1px solid #eee">{title}{variant_html}</td>'
            f'<td style="padding:10px 6px;text-align:center;border-bottom:1px solid #eee;color:#666">{qty}</td>'
            f'<td style="padding:10px 0;text-align:right;border-bottom:1px solid #eee">${price:.2f}</td>'
            f'<td style="padding:10px 0 10px 8px;text-align:right;border-bottom:1px solid #eee;font-weight:600">${line:.2f}</td>'
            f'</tr>'
        )
        items_text_lines.append(
            f"  - {title}{variant_text}  x{qty}  ${line:.2f} {currency}")

    items_html = f"""
      <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.06em;color:#888;margin:24px 0 8px">Order Summary</h3>
      <table style="border-collapse:collapse;width:100%;font-size:14px;margin:0 0 16px">
        <thead>
          <tr style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:0.04em">
            <th style="text-align:left;padding:6px 0;border-bottom:2px solid #ddd;font-weight:500">Product</th>
            <th style="text-align:center;padding:6px 0;border-bottom:2px solid #ddd;font-weight:500">Qty</th>
            <th style="text-align:right;padding:6px 0;border-bottom:2px solid #ddd;font-weight:500">Price</th>
            <th style="text-align:right;padding:6px 0;border-bottom:2px solid #ddd;font-weight:500">Total</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>""" if items else ""

    discount_row_html = ""
    discount_text = ""
    if promo_amount > 0:
        code_label = f' (<strong>{html_lib.escape(discount_code)}</strong>)' if discount_code else ""
        p_pct_label = f" &mdash; {promo_pct:.0f}%" if promo_pct > 0 else ""
        discount_row_html += (f'<tr><td style="padding:6px 0;color:#1a7e2e">Promo discount{code_label}{p_pct_label}</td>'
                              f'<td style="padding:6px 0;text-align:right;color:#1a7e2e">-${promo_amount:.2f} {currency}</td></tr>')
        discount_text += f"Promo discount{' (' + discount_code + ')' if discount_code else ''}{' - ' + str(int(promo_pct)) + '%' if promo_pct else ''}: -${promo_amount:.2f} {currency}\n"
    if method_amount > 0:
        mlabel = {"interac": "Interac e-Transfer discount", "zelle": "Zelle discount",
                  "crypto": "Crypto discount", "altcoin": "Crypto discount"}.get(pm_value, "Discount")
        m_pct_label = f" &mdash; {method_pct:.0f}%" if method_pct > 0 else ""
        discount_row_html += (f'<tr><td style="padding:6px 0;color:#1a7e2e">{mlabel}{m_pct_label}</td>'
                              f'<td style="padding:6px 0;text-align:right;color:#1a7e2e">-${method_amount:.2f} {currency}</td></tr>')
        discount_text += f"{mlabel}{' - ' + str(int(method_pct)) + '%' if method_pct else ''}: -${method_amount:.2f} {currency}\n"

    addr_parts = [
        f"{order.first_name or ''} {order.last_name or ''}".strip(),
        order.address1 or "", order.address2 or "",
        ", ".join(
            filter(None, [order.city, order.province, order.postal_code])),
        order.country or "",
    ]
    addr_lines = [p for p in addr_parts if p]
    addr_text = "\n    ".join(addr_lines)
    # HTML version escapes each line separately — addr_text above stays raw
    # since plain text doesn't need (or want) HTML entities.
    addr_html = "<br>".join(html_lib.escape(p) for p in addr_lines)

    body_html = f"""
      <div style="background:{accent};color:#fff;padding:14px 18px;border-radius:6px;margin-bottom:24px">
        <span style="font-size:18px;font-weight:700">&#10003; Order Confirmed</span>
        <span style="float:right;font-size:13px;opacity:0.85">{ref_label}</span>
      </div>
      <p>Hi {html_lib.escape(order.first_name) if order.first_name else 'there'},</p>
      <p>Your payment has been received and your order is confirmed. We'll get it packed and on its way shortly. Please keep in mind that we are a medical facility, so it may take an additional 1–2 business days to process your order, especially if we have received a high volume of orders recently. Feel free to reach out to our customer service team through the website at any time, and they will be happy to assist you.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0">
        <tr><td style="padding:6px 0;color:#666;width:40%">Order number</td><td style="padding:6px 0;font-weight:600">{order.id}</td></tr>
        <tr><td style="padding:6px 0;color:#666">Order reference</td><td style="padding:6px 0;font-weight:600">{ref_label}</td></tr>
        <tr><td style="padding:6px 0;color:#666">Store</td><td style="padding:6px 0">{store_html}</td></tr>
        <tr><td style="padding:6px 0;color:#666">Payment method</td><td style="padding:6px 0">{method_lbl}</td></tr>
      </table>
      {items_html}
      <table style="border-collapse:collapse;margin:8px 0 16px;width:100%;font-size:14px">
        <tr><td style="padding:6px 0;color:#666">Subtotal</td><td style="padding:6px 0;text-align:right">${subtotal:.2f} {currency}</td></tr>
        {discount_row_html}
        <tr style="border-top:2px solid {accent}">
          <td style="padding:10px 0;font-weight:bold">Total paid</td>
          <td style="padding:10px 0;text-align:right;font-weight:bold;color:{accent}">${total:.2f} {currency}</td>
        </tr>
        {f'<tr><td colspan="2" style="padding:0 0 6px;text-align:right;font-size:12px;color:#888">Charged as ${settled_amount:.2f} {settled_currency} on your card</td></tr>' if show_usd_charge else ''}
      </table>
      <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.06em;color:#888;margin:24px 0 8px">Shipping To</h3>
      <div style="background:#f7f7f7;padding:12px 14px;border-radius:4px;font-size:14px;line-height:1.7">{addr_html}</div>
      <p style="font-size:14px;color:#555;margin-top:20px">Questions? Use the chat widget on our website.</p>
    """

    items_block = ("Order summary:\n" + "\n".join(items_text_lines) +
                   "\n\n") if items_text_lines else ""
    usd_charge_text = f"Charged as: ${settled_amount:.2f} {settled_currency} on your card\n" if show_usd_charge else ""
    text = (
        f"Hi {order.first_name or 'there'},\n\nYour order is confirmed and payment received.\n\n"
        f"Order number:    {order.id}\nOrder reference: {ref_label}\nStore:           {store_label}\nPayment method:  {method_lbl}\n\n"
        f"{items_block}"
        f"Subtotal:   ${subtotal:.2f} {currency}\n{discount_text}Total paid: ${total:.2f} {currency}\n{usd_charge_text}\n"
        f"Ship to:\n    {addr_text}\n\nQuestions? Use the chat widget on our website.\n"
    )
    return {"subject": subject, "html": _wrap_html(body_html), "text": text}


async def send_confirmation_email(
    order,
    shopify_order_number: Optional[str] = None,
    accent: str = "#dd1d1d",
) -> Optional[dict]:
    """
    Returns None only when there's no email on the order to send to (nothing
    attempted). Otherwise returns the template used plus whether the send
    actually succeeded — callers (services/order_finalize.py) use this to
    write a CustomerEmailLog audit row regardless of outcome, not just on
    success, so failed sends are visible too.
    """
    if not order.email:
        logger.warning(
            "send_confirmation_email: no email on order %s", order.id)
        return None
    tpl = build_confirmation_template(
        order, shopify_order_number, accent="#2a7a2a")
    ok = await send_email(to=order.email, subject=tpl["subject"], html=tpl["html"], text=tpl.get("text"))
    if ok:
        logger.info("Confirmation email sent -> %s | order %s",
                    order.email, order.id)
    return {"success": ok, **tpl}


async def send_order_received_email(order, accent: str = "#dd1d1d") -> Optional[dict]:
    """
    Sent immediately when an Interac or Zelle order is placed
    (routes/checkout.py checkout_interac/checkout_zelle) — separate from
    send_confirmation_email, which only fires once the payment is actually
    matched. Never raises; a failed send here should not block the checkout
    response.
    """
    if not order.email:
        logger.warning(
            "send_order_received_email: no email on order %s", order.id)
        return None
    tpl = build_order_received_template(order, accent)
    ok = await send_email(to=order.email, subject=tpl["subject"], html=tpl["html"], text=tpl.get("text"))
    if ok:
        logger.info("Order-received email sent -> %s | order %s",
                    order.email, order.id)
    return {"success": ok, **tpl}
