"""
POST /api/checkout/card      → Helcim credit card
POST /api/checkout/interac   → Interac e-Transfer (create pending order)
POST /api/checkout/crypto    → BTCPay Server invoice
GET  /api/checkout/status/{order_id}
"""
import logging
import re
import time
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.order import Order, OrderItem, InteracPayment, CryptoInvoice, ZellePayment, NowPaymentsInvoice, PaymentMethod, PaymentStatus
from models.brand import Brand
from services.order_id import generate_order_id
from services.helcim import HelcimClient, HelcimError
from services.btcpay import BTCPayClient, BTCPayError
from config import settings
from services.shopify_draft import create_draft_order, ShopifyError
from services.nowpayments import NowPaymentsClient, NowPaymentsError
from services.pymtz import PymtzClient, PymtzError

router  = APIRouter(prefix="/api/checkout", tags=["checkout"])
logger  = logging.getLogger(__name__)

# pymtz settles in USD, so CAD orders must be converted before we send the
# amount. This MUST stay in sync with USD_CONVERSION_RATE in checkout.html
# (the "≈ $X USD" badge), or the charge won't match what the customer was shown.
USD_CONVERSION_RATE = 1.4  # 1 USD = 1.4 CAD


# ─── Abuse guard on payment-creating endpoints ─────────────────────────────────
# Added after a direct-POST bypass was caught hitting /api/checkout/card
# across 6 different stores in ~35 seconds with the same fake identity
# (t@e.com / "T E") — that endpoint had no gate at all (now fixed separately)
# AND nothing here would have slowed down rapid scripted hits even once
# gated per-store, since store allowlists don't limit *rate*. This is a
# simple per-IP sliding-window counter in Redis, applied to every endpoint
# that actually creates an order/payment session (not /reserve or the
# autosave endpoint, which fire innocuously on every page load/keystroke and
# don't need this). Loose enough that a real shopper submitting one checkout
# never notices it.
CHECKOUT_RATE_LIMIT_MAX    = 8    # submissions...
CHECKOUT_RATE_LIMIT_WINDOW = 60   # ...per this many seconds, per IP


def _client_ip(request: Request) -> str:
    """
    request.client.host is the proxy's IP (this app sits behind
    Cloudflare/nginx), not the visitor's. Prefer Cloudflare's
    CF-Connecting-IP header, then the first hop of X-Forwarded-For, then
    fall back to request.client.host (e.g. local/direct testing).
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _rate_limit_submission(request: Request) -> None:
    """Dependency — raise 429 if this IP has submitted too many checkout
    attempts too quickly. Fails OPEN (allows the request) on any Redis
    error, so an infra hiccup never blocks real checkouts."""
    try:
        from routes.auth_routes import get_redis
        r = await get_redis()
        key = f"checkout_ratelimit:{_client_ip(request)}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, CHECKOUT_RATE_LIMIT_WINDOW)
        if count > CHECKOUT_RATE_LIMIT_MAX:
            raise HTTPException(429, "Too many checkout attempts — please wait a minute and try again.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[rate-limit] check failed, allowing request: {e}")


def _v2_referer_suffix(request: Request) -> str:
    """
    Build a "?v=2&country=XX" suffix from the request's Referer header so
    server-side redirect URLs (pymtz return_url, NowPayments success_url) can
    forward the v2 reskin flag + country palette onto the confirmation page.

    Returns "" if neither flag is present.
    """
    ref = request.headers.get("referer") or ""
    parts = []
    if "v=2" in ref:
        parts.append("v=2")
    m = re.search(r"[?&]country=([A-Za-z]{2})", ref)
    if m:
        parts.append(f"country={m.group(1).upper()}")
    return ("?" + "&".join(parts)) if parts else ""


# ─── Shared input schemas ─────────────────────────────────────────────────────

class CartItem(BaseModel):
    product_id: str | None = Field(None, max_length=50)
    title:      str        = Field(..., min_length=1, max_length=500)
    variant:    str | None = Field(None, max_length=200)
    qty:        int        = Field(1, ge=1, le=100)
    price:      float      = Field(..., ge=0, le=10000)
    # Product image URL (Shopify CDN). Optional. Used to render the actual
    # product thumbnail in the v2 confirmation pages.
    image:      str | None = Field(None, max_length=500)


class CheckoutBase(BaseModel):
    # Optional pre-reserved order ID (from /api/checkout/reserve on page load)
    order_id: str | None = None

    # Contact
    email:      str
    first_name: str | None = None
    last_name:  str
    phone:      str | None = None

    # Shipping
    address1:    str | None = None
    address2:    str | None = None
    city:        str | None = None
    province:    str | None = None
    postal_code: str | None = None
    country:     str        = "CA"

    # Billing
    bill_same:     str = "1"
    bill_address1: str | None = None
    bill_address2: str | None = None
    bill_city:     str | None = None
    bill_province: str | None = None
    bill_postal:   str | None = None
    bill_country:  str | None = None

    # Cart (JSON-encoded from frontend)
    items: list[CartItem] = Field(default_factory=list)

    # Totals (we validate server-side)
    subtotal: float
    currency: str = "CAD"
    source_domain: str | None = None
    store_name: str | None = None   # friendly store name from the ?storename= URL param (for display)
    store_country: str = "CA"   # "CA" or "US" — which store the order came from (not shipping)

    # Discount info — applies to all payment methods
    discount_code: str | None = None
    discount_amount: float = 0.0
    payment_method_discount: float = 0.0

    # Optional password for the "soft account" prefill feature. When set,
    # we upsert a row in customer_accounts (email + pbkdf2 hash + saved
    # profile) so the customer can sign in on a return visit and have all
    # their fields prefilled. The plaintext password never reaches the
    # orders table.
    account_password: str | None = Field(None, min_length=5, max_length=64)

    # Optional business-info fields — currently only Princeton's checkout
    # renders the "Research Information" section. Empty on all other stores.
    company:        str | None = Field(None, max_length=255)
    research_field: str | None = Field(None, max_length=100)


class CardCheckoutRequest(CheckoutBase):
    helcim_pay_token: str | None = None


class StripeDirectCheckoutRequest(CheckoutBase):
    """Payload for POST /api/checkout/stripe_direct — card paid via Stripe."""
    # PaymentMethod ID from Stripe Elements (e.g. "pm_1xxxxx...").
    # Stripe.js tokenizes the card in the browser; we only see this ID.
    payment_method_id: str


class InteracCheckoutRequest(CheckoutBase):
    pass


class CryptoCheckoutRequest(CheckoutBase):
    pass


class ReserveRequest(BaseModel):
    """Payload for POST /api/checkout/reserve — creates a bare pending order."""
    items:         list[CartItem] = Field(default_factory=list)
    subtotal:      float
    currency:      str = "CAD"
    source_domain: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_brand(request: Request) -> Brand | None:
    return getattr(request.state, "brand", None)


def _compute_total(subtotal: float, discount_pct: float) -> tuple[float, float]:
    discount_amount = round(subtotal * discount_pct / 100, 2)
    total = round(subtotal - discount_amount, 2)
    return discount_amount, total


def _safe_pct(amount: float, subtotal: float) -> float:
    """Compute discount % from amount + subtotal. Returns 0 if subtotal is 0."""
    sub = float(subtotal or 0)
    amt = float(amount or 0)
    if sub <= 0 or amt <= 0:
        return 0.0
    return round((amt / sub) * 100, 2)

MAX_ITEMS_PER_ORDER = 50
MAX_TOTAL_ORDER     = 100000.0


def _validate_cart(items: list, claimed_subtotal: float, promo_discount: float = 0.0) -> None:
    """
    Verify the claimed cart subtotal matches what the line items add up to,
    so a tampered checkout payload can't ship cheap.

    Two payload shapes are accepted (different Shopify themes do it differently):
      A) Items at original unit price, subtotal lowered by the promo savings.
         Customer-typed discount on our checkout page produces this shape.
         Match: sum(items) - promo_discount == subtotal
      B) Items already pre-discounted by the source theme, subtotal == sum(items).
         URL-applied discount (?discount=CODE from a theme that pre-discounts
         line item prices) produces this shape.
         Match: sum(items) == subtotal
    """
    if not items:
        raise HTTPException(400, "Cart is empty")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise HTTPException(400, "Too many items in cart")

    computed = 0.0
    for it in items:
        computed += float(it.price) * int(it.qty)
    computed = round(computed, 2)

    if computed > MAX_TOTAL_ORDER:
        raise HTTPException(400, "Order total exceeds maximum")

    claimed = round(float(claimed_subtotal), 2)

    # Shape A: items at full price, subtotal already reduced by discount_amount
    expected_A = round(computed - max(0.0, float(promo_discount or 0.0)), 2)
    # Shape B: items already pre-discounted, subtotal equals items sum
    expected_B = computed

    if abs(expected_A - claimed) > 0.01 and abs(expected_B - claimed) > 0.01:
        raise HTTPException(400, "Cart total mismatch")


async def _create_base_order(
    db: AsyncSession,
    data: CheckoutBase,
    payment_method: PaymentMethod,
    brand: Brand | None,
    discount_pct: float,
    request: Request,
) -> Order:
    """
    Create OR update an Order + OrderItems. Returns the Order.

    If data.order_id is provided AND matches an existing pending order,
    we UPDATE that row instead of inserting a new one. This prevents
    duplicate orders when a customer switches payment methods, double-clicks,
    or refreshes the checkout page.
    """
    # Prefer the friendly storename passed from checkout (?storename=); fall
    # back to the brand, then a generic label.
    store_name = (data.store_name or "").strip() or (brand.store_name if brand else "Checkout")
    discount_amount, total = _compute_total(data.subtotal, discount_pct)

    # Enforce store-pinned currency. If this v2 store has a country fixed in
    # data/checkout_v2_stores.txt (`domain:US` / `domain:CA`), we ALWAYS use
    # that store's currency — protects against a theme that didn't pass
    # `&country=US` and would otherwise default to CAD. Non-listed stores
    # keep the payload-supplied currency.
    try:
        from main import _v2_store_country
        _src = data.source_domain or request.query_params.get("source") or request.headers.get("host", "")
        _pinned = _v2_store_country(_src)
        if _pinned == "US":
            data.currency = "USD"
        elif _pinned == "CA":
            data.currency = "CAD"
    except Exception:
        pass  # never block order creation on the currency-enforcement check

    # Try to reuse an existing reserved order
    order: Order | None = None
    if data.order_id:
        result = await db.execute(select(Order).where(Order.id == data.order_id))
        order  = result.scalar_one_or_none()
        # Guard: only reuse if it's still pending — don't touch paid/failed/cancelled
        if order and order.payment_status != PaymentStatus.pending:
            order = None
        # If switching payment method on a pending order, wipe stale payment-method
        # rows so we don't leave orphaned InteracPayment/ZellePayment/CryptoInvoice
        # records pointing at this order under a method the customer abandoned.
        if order and order.payment_method != payment_method:
            await db.execute(
                InteracPayment.__table__.delete().where(InteracPayment.order_id == order.id)
            )
            await db.execute(
                ZellePayment.__table__.delete().where(ZellePayment.order_id == order.id)
            )
            await db.execute(
                CryptoInvoice.__table__.delete().where(CryptoInvoice.order_id == order.id)
            )

    if order:
        # UPDATE path — reuse the reserved order
        order.brand_id        = brand.id if brand else order.brand_id or 1
        order.store_name      = store_name
        order.email           = data.email
        order.first_name      = data.first_name
        order.last_name       = data.last_name
        order.address1        = data.address1
        order.address2        = data.address2
        order.city            = data.city
        order.province        = data.province
        order.postal_code     = data.postal_code
        order.country         = data.country
        order.bill_same       = data.bill_same
        order.bill_address1   = data.bill_address1
        order.bill_address2   = data.bill_address2
        order.bill_city       = data.bill_city
        order.bill_province   = data.bill_province
        order.bill_postal     = data.bill_postal
        order.bill_country    = data.bill_country
        # Compute original (pre-promo) subtotal — needed for accurate email display
        promo_amt   = float(data.discount_amount or 0)
        post_promo  = float(data.subtotal or 0)
        original_sub = round(post_promo + promo_amt, 2) if promo_amt > 0 else post_promo
        promo_pct_calc = round((promo_amt / original_sub) * 100, 2) if original_sub > 0 and promo_amt > 0 else 0.0

        order.subtotal              = Decimal(str(post_promo))
        order.original_subtotal     = Decimal(str(original_sub))
        order.discount_code         = data.discount_code
        order.promo_discount_amount = Decimal(str(promo_amt))
        order.promo_discount_pct    = Decimal(str(promo_pct_calc))
        order.discount_pct          = Decimal(str(discount_pct))
        order.discount_amount       = Decimal(str(discount_amount))
        order.total                 = Decimal(str(total))
        order.currency        = data.currency
        order.payment_method  = payment_method
        order.ip_address      = request.client.host if request.client else None
        order.user_agent      = request.headers.get("user-agent", "")
        order.visitor_id      = request.cookies.get("cs_vid") or order.visitor_id
        # Princeton-only fields — will be None on any other store.
        order.company         = (data.company or None) or order.company
        order.research_field  = (data.research_field or None) or order.research_field
        if data.source_domain:
            order.source_domain = data.source_domain

        # Replace line items — delete existing, re-add from payload
        await db.execute(
            OrderItem.__table__.delete().where(OrderItem.order_id == order.id)
        )
        for item in data.items:
            db.add(OrderItem(
                order_id       = order.id,
                product_id     = item.product_id,
                title          = item.title,
                variant        = item.variant,
                qty            = item.qty,
                price          = Decimal(str(item.price)),
                original_price = Decimal(str(getattr(item, "original_price", None) or item.price)),
                total          = Decimal(str(round(item.price * item.qty, 2))),
                image_url      = getattr(item, "image", None),
            ))

        await db.flush()
        await _maybe_upsert_customer_account(db, data)
        return order

    # INSERT path — no reserved order, create fresh
    order_id = generate_order_id()
    while True:
        existing = await db.execute(select(Order).where(Order.id == order_id))
        if not existing.scalar_one_or_none():
            break
        order_id = generate_order_id()

    # Compute promo math for INSERT path
    promo_amt_i    = float(data.discount_amount or 0)
    post_promo_i   = float(data.subtotal or 0)
    original_sub_i = round(post_promo_i + promo_amt_i, 2) if promo_amt_i > 0 else post_promo_i
    promo_pct_i    = round((promo_amt_i / original_sub_i) * 100, 2) if original_sub_i > 0 and promo_amt_i > 0 else 0.0

    order = Order(
        id              = order_id,
        brand_id        = brand.id if brand else 1,
        store_name      = store_name,
        email           = data.email,
        first_name      = data.first_name,
        last_name       = data.last_name,
        address1        = data.address1,
        address2        = data.address2,
        city            = data.city,
        province        = data.province,
        postal_code     = data.postal_code,
        country         = data.country,
        bill_same       = data.bill_same,
        bill_address1   = data.bill_address1,
        bill_address2   = data.bill_address2,
        bill_city       = data.bill_city,
        bill_province   = data.bill_province,
        bill_postal     = data.bill_postal,
        bill_country    = data.bill_country,
        subtotal              = Decimal(str(post_promo_i)),
        original_subtotal     = Decimal(str(original_sub_i)),
        discount_code         = data.discount_code,
        promo_discount_amount = Decimal(str(promo_amt_i)),
        promo_discount_pct    = Decimal(str(promo_pct_i)),
        discount_pct          = Decimal(str(discount_pct)),
        discount_amount       = Decimal(str(discount_amount)),
        total                 = Decimal(str(total)),
        currency        = data.currency,
        payment_method  = payment_method,
        payment_status  = PaymentStatus.pending,
        ip_address      = request.client.host if request.client else None,
        user_agent      = request.headers.get("user-agent", ""),
        source_domain   = data.source_domain or request.query_params.get("source") or request.headers.get("host", ""),
        visitor_id      = request.cookies.get("cs_vid"),
        # Princeton-only fields — will be None on any other store.
        company         = data.company or None,
        research_field  = data.research_field or None,
    )
    db.add(order)

    for item in data.items:
        db.add(OrderItem(
            order_id       = order_id,
            product_id     = item.product_id,
            title          = item.title,
            variant        = item.variant,
            qty            = item.qty,
            price          = Decimal(str(item.price)),
            original_price = Decimal(str(getattr(item, "original_price", None) or item.price)),
            total          = Decimal(str(round(item.price * item.qty, 2))),
            image_url      = getattr(item, "image", None),
        ))

    await db.flush()
    await _maybe_upsert_customer_account(db, data)
    return order


async def _maybe_upsert_customer_account(db: AsyncSession, data: "CheckoutBase") -> None:
    """
    If the customer typed a password in Section 5, upsert their account row
    (email + hash + saved profile) so a return visit can prefill the form
    via /api/customer/lookup. Best-effort — any failure is swallowed.

    Runs INSIDE the same DB transaction as the order create so the row is
    only committed if the order itself commits. That keeps us from leaving
    orphan customer rows when the customer abandons before paying.
    """
    pwd = (getattr(data, "account_password", None) or "").strip()
    if not pwd:
        return
    try:
        from services.customer_accounts import upsert_account
        await upsert_account(
            db,
            email    = data.email or "",
            password = pwd,
            profile  = {
                "first_name":  data.first_name  or "",
                "last_name":   data.last_name   or "",
                "phone":       data.phone       or "",
                "address1":    data.address1    or "",
                "address2":    data.address2    or "",
                "city":        data.city        or "",
                "province":    data.province    or "",
                "postal_code": data.postal_code or "",
                "country":     data.country     or "",
            },
        )
    except Exception as e:
        logger.warning(f"[customer_accounts] upsert hook failed for {data.email!r}: {e}")


# ─── POST /api/checkout/reserve ──────────────────────────────────────────────

@router.post("/reserve")
async def checkout_reserve(
    payload: ReserveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Reserves an order ID on page load. Creates a bare pending Order with items
    but no customer details yet. The customer's details are filled in later
    when they submit via /api/checkout/{card,interac,crypto}.

    This prevents duplicate orders when the customer switches payment methods
    or double-clicks Pay Now.
    """
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    brand = _get_brand(request)

    order_id = generate_order_id()
    while True:
        existing = await db.execute(select(Order).where(Order.id == order_id))
        if not existing.scalar_one_or_none():
            break
        order_id = generate_order_id()

    order = Order(
        id             = order_id,
        brand_id       = brand.id if brand else 1,
        store_name     = brand.store_name if brand else "Checkout",
        email          = "",                            # filled on submit
        first_name     = None,
        last_name      = "",                            # filled on submit
        subtotal       = Decimal(str(payload.subtotal)),
        total          = Decimal(str(payload.subtotal)),  # before discount
        discount_pct   = Decimal("0"),
        discount_amount= Decimal("0"),
        currency       = payload.currency,
        payment_method = PaymentMethod.card,            # placeholder, overwritten on submit
        payment_status = PaymentStatus.pending,
        ip_address     = request.client.host if request.client else None,
        user_agent     = request.headers.get("user-agent", ""),
        source_domain  = payload.source_domain or request.query_params.get("source") or request.headers.get("host", ""),
        visitor_id     = request.cookies.get("cs_vid"),
    )
    db.add(order)

    for item in payload.items:
        db.add(OrderItem(
            order_id   = order_id,
            product_id = item.product_id,
            title      = item.title,
            variant    = item.variant,
            qty        = item.qty,
            price      = Decimal(str(item.price)),
            total      = Decimal(str(round(item.price * item.qty, 2))),
        ))

    await db.commit()
    logger.info(f"Reserved order {order_id}")
    return {"order_id": order_id}


# ─── POST /api/checkout/update ───────────────────────────────────────────────
# Auto-save individual form fields as the customer types. Frontend calls this
# on field blur (and email keystroke, debounced) so we capture customer info
# progressively. Protects against customers who pay externally (e.g. via
# Interac e-Transfer) but forget to click Place Order — we still have their
# email/name/address to match the payment to a customer and ship the order.

class AutoSaveRequest(BaseModel):
    order_id: str = Field(..., max_length=50)
    field:    str = Field(..., max_length=50)
    value:    str = Field(..., max_length=255)


# Whitelist of frontend field names → DB column names.
# Anything not in this map is rejected.
AUTOSAVE_FIELD_MAP = {
    "email":          "email",
    "firstName":      "first_name",
    "lastName":       "last_name",
    "phone":          "phone",
    "address1":       "address1",
    "address2":       "address2",
    "city":           "city",
    "zone":           "province",
    "postalCode":     "postal_code",
    "country":        "country",
    "billAddress1":   "bill_address1",
    "billCity":       "bill_city",
    "billZone":       "bill_province",
    "billPostal":     "bill_postal",
    "billCountry":    "bill_country",
    "payment_method": "payment_method",
}


@router.post("/update")
async def autosave_order_field(
    payload: AutoSaveRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-save a single field of a reserved order as the customer types.
    Fire-and-forget — never blocks the customer, never raises HTTP errors.

    Behavior:
      • Empty values rejected (we never overwrite with empty)
      • Order must be in pending state (won't touch paid/failed/cancelled)
      • Only whitelisted fields are accepted (security)
      • payment_method strings are converted to the PaymentMethod enum
      • Any DB error is swallowed silently and logged
    """
    try:
        order_id = payload.order_id.strip()
        js_field = payload.field.strip()
        v        = payload.value.strip()

        if not order_id or not js_field or not v:
            return {"ok": False, "error": "missing_or_empty"}

        # Map JS field name → DB column. Reject unknown fields.
        db_column = AUTOSAVE_FIELD_MAP.get(js_field)
        if not db_column:
            return {"ok": False, "error": "unknown_field"}

        # Look up the reserved order
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            return {"ok": False, "error": "order_not_found"}

        # Only allow updates while the order is still pending
        if order.payment_status != PaymentStatus.pending:
            return {"ok": False, "error": "order_locked"}

        # Special handling for payment_method — recompute discount + total too
        # so the admin dashboard reflects the right amount as customer changes
        # methods (each method has a different discount %).
        if db_column == "payment_method":
            try:
                new_method = PaymentMethod(v)
            except (ValueError, KeyError):
                return {"ok": False, "error": "invalid_payment_method"}

            # Look up the brand for discount percentages (default fallbacks if not set)
            brand_result = await db.execute(
                select(Brand).where(Brand.id == order.brand_id)
            )
            brand = brand_result.scalar_one_or_none()

            if new_method == PaymentMethod.interac:
                pct = float(brand.interac_discount) if brand and brand.interac_discount else 10.0
            elif new_method == PaymentMethod.zelle:
                pct = float(getattr(brand, "zelle_discount", None) or 10.0)
            elif new_method == PaymentMethod.crypto:
                pct = float(brand.crypto_discount) if brand and brand.crypto_discount else 10.0
            elif new_method == PaymentMethod.altcoin:
                pct = 7.0   # NowPayments altcoin discount — matches /altcoin endpoint
            else:  # card
                pct = 0.0

            sub        = float(order.subtotal or 0)
            disc_amt   = round(sub * pct / 100, 2)
            new_total  = round(sub - disc_amt, 2)

            try:
                order.payment_method  = new_method
                order.discount_pct    = Decimal(str(pct))
                order.discount_amount = Decimal(str(disc_amt))
                order.total           = Decimal(str(new_total))
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning(f"[autosave] payment_method update failed for {order_id}: {e}")
                return {"ok": False, "error": "db_error"}

            return {"ok": True}

        # All other fields — simple setattr
        try:
            setattr(order, db_column, v)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning(f"[autosave] DB update failed for {order_id}.{db_column}: {e}")
            return {"ok": False, "error": "db_error"}

        return {"ok": True}

    except Exception as e:
        # Catch-all — never throw, always return ok:false silently
        logger.warning(f"[autosave] unexpected error: {e}")
        return {"ok": False, "error": "server_error"}


# ─── POST /api/checkout/card ─────────────────────────────────────────────────

@router.post("/card", dependencies=[Depends(_rate_limit_submission)])
async def checkout_card(
    payload: CardCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Credit card via pymtz hosted payment page.

    Creates the pending order, then creates a pymtz payment intent and returns
    the hosted payment_url. The frontend redirects the customer there; pymtz
    fires /webhooks/pymtz on completion, which marks the order paid and
    triggers Shopify order creation + affiliate webhook.
    """
    # Card (pymtz) is blanket-disabled for every order right now — the same
    # business decision main.py's checkout_page() applies before it even
    # renders the "Card" radio button (see the
    # `if country in ("US", "CA"): card_enabled = False` block there, which
    # covers every possible order since this system only ever handles US/CA).
    # That override only hid the button on the checkout PAGE — this endpoint
    # had no matching server-side check, unlike every other card processor
    # (Auth.net, Stripe Direct both gate themselves inline). A direct POST
    # here bypassed the disable entirely and still created real pymtz
    # payment sessions — confirmed happening in production. To re-enable,
    # remove this block AND the matching one in main.py, and add the same
    # per-store/country gate the other card processors already have instead
    # of leaving this endpoint wide open again.
    raise HTTPException(503, "Credit card payment is currently disabled")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    description = f"Order {order.id}"
    if payload.items:
        first = payload.items[0].title
        extra = len(payload.items) - 1
        description = first + (f" +{extra} more" if extra > 0 else "")

    # Server-side redirects from pymtz can't see client-side state, so inject
    # v2 + country into return_url here based on the Referer.
    return_url = f"{settings.BASE_URL}/order/{order.id}/confirmation{_v2_referer_suffix(request)}"
    cancel_url = f"{settings.BASE_URL}/"

    # pymtz settles in USD. Convert CAD totals so the customer is charged the
    # USD equivalent shown by the "≈ $X USD" badge on checkout — not the raw
    # CAD number relabeled as USD.
    if (order.currency or "").upper() == "CAD":
        pymtz_amount   = round(float(order.total) / USD_CONVERSION_RATE, 2)
        pymtz_currency = "USD"
        # Admin-facing only — order.total/currency stay the CAD cart price
        # on purpose (see models/order.py settled_currency/settled_amount).
        order.settled_currency = pymtz_currency
        order.settled_amount   = pymtz_amount
    else:
        pymtz_amount   = float(order.total)
        pymtz_currency = order.currency

    try:
        pymtz_country = "US" if (order.currency or "").upper() == "USD" else "CA"
        client  = PymtzClient(country=pymtz_country)

        # Use billing address if the customer entered one, else fall back to
        # shipping. payload.bill_same == "1" means "billing == shipping".
        bill_same = (payload.bill_same or "1") == "1"
        b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
        b_addr2   = (payload.address2 if bill_same else payload.bill_address2) or ""
        b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
        b_state   = (payload.province if bill_same else payload.bill_province) or ""
        b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
        b_country = (payload.country  if bill_same else payload.bill_country)  or "CA"

        payment = await client.create_payment(
            order_id    = order.id,
            amount      = pymtz_amount,
            currency    = pymtz_currency,
            description = description,
            email       = payload.email,
            return_url  = return_url,
            cancel_url  = cancel_url,
            first_name  = payload.first_name or "",
            last_name   = payload.last_name  or "",
            phone       = payload.phone      or "",
            address1    = b_addr1,
            address2    = b_addr2,
            city        = b_city,
            state       = b_state,
            postal_code = b_zip,
            country     = b_country,
            metadata    = {
                "source_domain":    payload.source_domain or "",
                "store_name":       order.store_name or "",
                "order_currency":   order.currency or "",
                "order_total_cad":  str(order.total),
                "pymtz_account":    pymtz_country,
            },
        )

        order.payment_ref   = payment.get("id", "")
        order.payment_notes = f"pymtz payment {payment.get('id', '')} → {payment.get('payment_url', '')}"
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": payment["payment_url"],
            "paymentId":   payment.get("id", ""),
        }

    except PymtzError as e:
        logger.exception(f"pymtz payment creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = str(e)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")


# ─── POST /api/checkout/stripe_direct ────────────────────────────────────────
#
# Customer-facing endpoint for the "Credit Card (Stripe)" option — its own
# card processor, not a fallback. Customer's card is tokenized via Stripe
# Elements in the browser; we receive the PaymentMethod ID and
# create+confirm a PaymentIntent server-side.
#
# Test mode is controlled by the API KEY (sk_test_xxx vs sk_live_xxx) — no
# dashboard toggle. Set STRIPE_SECRET_KEY in .env appropriately.
#
# Cloaking discipline:
#   * Description sent to Stripe is neutral ("Order ORD-XXX"); no product names
#   * Metadata only contains internal IDs (order_id, source_domain)
#   * Auto-receipts to customer are disabled at Stripe dashboard level — we
#     send our own branded confirmation via Resend
#   * Statement descriptor (what customer sees on bank statement) is the
#     Stripe account's cloaked DBA — configured in dashboard, not here

@router.post("/stripe_direct", dependencies=[Depends(_rate_limit_submission)])
async def checkout_stripe_direct(
    payload: StripeDirectCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronous card charge via Stripe PaymentIntents API.

    Succeeds/fails on the SAME HTTP call. Returns:
      - success → mark paid, trigger Shopify + affiliate webhook, redirect
      - decline → 402 with bank reason
      - requires_action → 402 with the 3DS challenge URL (frontend handles)
    """
    from services.stripe_direct import StripeDirectClient, StripeError

    if not bool(getattr(settings, "STRIPE_DIRECT_ENABLED", False)):
        raise HTTPException(503, "Stripe direct not enabled")

    # Per-store gate: explicit allowlist.
    # ""    → no stores
    # "*"   → all stores
    # "..." → only listed domains
    _stripe_raw = (getattr(settings, "STRIPE_DIRECT_STORES", "") or "").strip()
    _src_domain = (payload.source_domain or "").strip().lower()
    if _stripe_raw == "*":
        _stripe_allowed = True
    elif _stripe_raw == "":
        _stripe_allowed = False
    else:
        _stripe_allowlist = {s.strip().lower() for s in _stripe_raw.split(",") if s.strip()}
        _stripe_allowed = _src_domain in _stripe_allowlist
    if not _stripe_allowed:
        raise HTTPException(403, f"Stripe not enabled for store '{_src_domain}'")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # Create / fetch the pending order — same pattern as the other card paths.
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    client_ip = request.client.host if request.client else None

    try:
        client = StripeDirectClient()
        result = await client.create_and_confirm_payment(
            payment_method_id = payload.payment_method_id,
            amount            = float(order.total),
            currency          = (order.currency or "USD"),
            order_id          = order.id,
            customer_email    = payload.email,
            customer_ip       = client_ip,
            source_domain     = payload.source_domain,
            # Neutral description — visible in Stripe dashboard only, but
            # we still keep it clean (no product names, no peptide refs).
            description       = f"Order {order.id}",
            # Use order ID as the idempotency key so accidental double-submit
            # from the same order won't double-charge.
            idempotency_key   = f"order-{order.id}",
        )
    except StripeError as e:
        logger.error(f"[stripe_direct] transport error for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"stripe error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Card payment failed: {e}")

    logger.info(
        f"[stripe_direct] order={order.id} status={result['status']} "
        f"pi={result['payment_intent_id']} msg={result['message'][:80]}"
    )

    # ── 3DS / SCA challenge required ────────────────────────────────────────
    # Stripe returned requires_action — the customer needs to complete a
    # 3DS challenge before we can finalize. Pass the next_action info back
    # to the frontend; Stripe.js handles the challenge UI.
    if result["status"] == "requires_action" and result.get("next_action"):
        order.payment_status = PaymentStatus.pending
        order.payment_ref    = f"pi:{result['payment_intent_id']}"
        order.payment_notes  = (
            f"stripe 3DS required · pi={result['payment_intent_id']}"
        )[:1000]
        await db.commit()
        return {
            "success":         False,
            "requires_action": True,
            "payment_intent_client_secret": result["raw"].get("client_secret", ""),
            "orderId":         order.id,
        }

    if not result["success"]:
        # Decline / error — surface to user, mark failed.
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = (
            f"stripe declined · status={result['status']} "
            f"pi={result['payment_intent_id']} · {result['message'][:200]}"
        )[:1000]
        await db.commit()
        raise HTTPException(402, detail={
            "code":    result["status"],
            "message": result["message"],
            "pi":      result["payment_intent_id"],
        })

    # ── Charge succeeded — mark paid + downstream Shopify/affiliate ────────
    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.now(timezone.utc)
    # Prefix `pi:` so the admin dashboard classifier recognizes this as
    # Stripe direct (see models/order.py:_classify_processor — `pi_` prefix
    # already maps to "stripe").
    order.payment_ref    = f"pi_{result['payment_intent_id'].replace('pi_', '')}"
    order.payment_notes  = (
        f"stripe paid · pi={result['payment_intent_id']} "
        f"charge={result['charge_id']} card={result['brand']}*{result['last4']}"
    )[:1000]
    await db.commit()
    logger.info(f"✅ Card payment confirmed (stripe_direct): order {order.id}")

    # Downstream — Shopify create + affiliate webhook, dispatched to Celery
    # instead of awaited inline, off the request path.
    from tasks.celery_app import run_post_payment_hooks
    run_post_payment_hooks.apply_async(args=[order.id, "stripe_direct"])

    return {
        "success":     True,
        "orderId":     order.id,
        "redirectUrl": f"/order/{order.id}/confirmation",
        "paymentId":   f"pi:{result['payment_intent_id']}",
    }


# ─── POST /api/checkout/wpay ──────────────────────────────────────────────────
#
# Customer-facing endpoint for the "WPay" option. Hosted-redirect card
# processor (HPP) — the customer enters card details on WPay's own page, not
# ours, so no raw card data ever reaches this backend. WPay is USD-only with
# a $20 minimum order; CAD totals are converted the same way /api/checkout/card
# converts for pymtz.

class WPayCheckoutRequest(CheckoutBase):
    pass


@router.post("/wpay", dependencies=[Depends(_rate_limit_submission)])
async def checkout_wpay(
    payload: WPayCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Card payment via WPay Channels' hosted payment page.

    Creates the pending order, then POSTs to WPay's hpp/request.php and
    returns the hosted redirectUrl. The frontend sends the customer there;
    WPay fires /webhooks/wpay on completion (with a 45-minute Celery poll as
    a fallback, per WPay's own guidance that callbacks can be missed).
    """
    from services.wpay import WPayClient, WPayError, MIN_AMOUNT as WPAY_MIN_AMOUNT

    if not bool(getattr(settings, "WPAY_ENABLED", False)):
        raise HTTPException(503, "WPay not enabled")

    # Per-store gate (mirrors STRIPE_DIRECT_STORES semantic).
    _wpay_raw   = (getattr(settings, "WPAY_STORES", "") or "").strip()
    _src_domain = (payload.source_domain or "").strip().lower()
    if _wpay_raw == "*":
        _wpay_allowed = True
    elif _wpay_raw == "":
        _wpay_allowed = False
    else:
        _wpay_allowlist = {s.strip().lower() for s in _wpay_raw.split(",") if s.strip()}
        _wpay_allowed = _src_domain in _wpay_allowlist
    if not _wpay_allowed:
        raise HTTPException(403, f"WPay not enabled for store '{_src_domain}'")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # Reuse the `card` discount rate (0.0) — WPay is a plain card-equivalent
    # option like Stripe Direct, not an incentivized manual method.
    order = await _create_base_order(db, payload, PaymentMethod.wpay, brand, 0.0, request)
    await db.commit()

    # WPay is USD-only. Convert CAD totals the same way checkout_card does
    # for pymtz, so the customer is charged the USD equivalent shown by the
    # "≈ $X USD" badge on checkout — not the raw CAD number relabeled as USD.
    if (order.currency or "").upper() == "CAD":
        wpay_amount = round(float(order.total) / USD_CONVERSION_RATE, 2)
        # Admin-facing only — order.total/currency stay the CAD cart price
        # on purpose (see models/order.py settled_currency/settled_amount).
        order.settled_currency = "USD"
        order.settled_amount   = wpay_amount
    else:
        wpay_amount = float(order.total)

    if wpay_amount < WPAY_MIN_AMOUNT:
        raise HTTPException(
            400,
            f"WPay requires a minimum order total of ${WPAY_MIN_AMOUNT:.2f} USD "
            f"(this order converts to ${wpay_amount:.2f} USD).",
        )

    # Billing address — same bill_same selection used by checkout_card.
    bill_same = (payload.bill_same or "1") == "1"
    b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
    b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
    b_state   = (payload.province if bill_same else payload.bill_province) or ""
    b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
    b_country = (payload.country  if bill_same else payload.bill_country)  or "US"

    transaction_id = f"{order.id}-{int(time.time())}"
    callback_url   = f"{settings.BASE_URL}/webhooks/wpay"
    redirect_url   = f"{settings.BASE_URL}/order/{order.id}/confirmation{_v2_referer_suffix(request)}"
    client_ip      = request.client.host if request.client else ""

    try:
        client = WPayClient()
        wpay_redirect_url = await client.submit_hpp_payment(
            transaction_id = transaction_id,
            amount         = wpay_amount,
            email          = payload.email,
            first_name     = payload.first_name or "",
            last_name      = payload.last_name  or "",
            phone          = payload.phone      or "",
            country        = b_country,
            state          = b_state,
            city           = b_city,
            address        = b_addr1,
            zip_code       = b_zip,
            ip_address     = client_ip,
            callback_url   = callback_url,
            redirect_url   = redirect_url,
        )

        order.payment_ref   = transaction_id
        order.payment_notes = f"wpay hpp txn={transaction_id} → {wpay_redirect_url}"
        await db.commit()

        # Kick off background polling as webhook fallback — WPay's own docs
        # say the fetch_record run window should be greater than 45 minutes.
        from tasks.celery_app import check_wpay_payment
        check_wpay_payment.apply_async(
            args=[order.id, transaction_id],
            countdown=2700,   # 45 minutes
        )

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": wpay_redirect_url,
            "paymentId":   f"wpay:{transaction_id}",
        }

    except WPayError as e:
        logger.error(f"[wpay] HPP request failed for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"wpay error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Could not start WPay payment: {e}")


# ─── POST /api/checkout/wpay_2d ───────────────────────────────────────────────
#
# Customer-facing endpoint for "Credit Card (WPay 2D)" — a SEPARATE option
# from /api/checkout/wpay (HPP) above, not a replacement for it. Routes
# through the same WordPress + WooCommerce site as onramp_wp, which has the
# real WPay Channels plugin installed — the plugin's own Basis Theory
# tokenization handles card entry, so raw card data never reaches this
# backend, same guarantee as the HPP flow, just via a different mechanism.

class WPay2DCheckoutRequest(CheckoutBase):
    pass


@router.post("/wpay_2d", dependencies=[Depends(_rate_limit_submission)])
async def checkout_wpay_2d(
    payload: WPay2DCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Card payment via the WPay Channels WooCommerce plugin (2D/direct-card
    mode), reached through the same WP site as the onramp_wp integration.

    Creates the pending order, then creates a WooCommerce order on that site
    with payment_method set to the WPay 2D gateway ID and returns its
    payment_url. The frontend redirects the customer there; WC fires
    /webhooks/wpay_2d on completion, which marks the order paid and triggers
    Shopify order creation + affiliate webhook — same downstream path as
    onramp_wp and every other card processor here.
    """
    from services.wpay_wp import WPayWPClient, WPayWPError, MIN_AMOUNT as WPAY_WP_MIN_AMOUNT

    if not bool(getattr(settings, "WPAY_WP_ENABLED", False)):
        raise HTTPException(503, "WPay (WP plugin) not enabled")

    # Per-store gate. A store qualifies by
    # exact domain (WPAY_WP_STORES) OR by country (WPAY_WP_COUNTRIES) —
    # either is sufficient, matching _wpay_2d_enabled_for() in main.py.
    _wpaywp_raw    = (getattr(settings, "WPAY_WP_STORES", "") or "").strip()
    _src_domain    = (payload.source_domain or "").strip().lower()
    if _wpaywp_raw == "*":
        _wpaywp_allowed = True
    else:
        _wpaywp_allowlist = {s.strip().lower() for s in _wpaywp_raw.split(",") if s.strip()}
        _wpaywp_allowed = _src_domain in _wpaywp_allowlist

    if not _wpaywp_allowed:
        _wpaywp_countries_raw = (getattr(settings, "WPAY_WP_COUNTRIES", "") or "").strip()
        _wpaywp_countries = {c.strip().upper() for c in _wpaywp_countries_raw.split(",") if c.strip()}
        _wpaywp_allowed = (getattr(payload, "store_country", "") or "").strip().upper() in _wpaywp_countries

    if not _wpaywp_allowed:
        raise HTTPException(403, f"WPay (WP plugin) not enabled for store '{_src_domain}'")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # Plain card-equivalent option — no incentive discount, matches wpay/stripe_direct.
    order = await _create_base_order(db, payload, PaymentMethod.wpay_2d, brand, 0.0, request)
    await db.commit()

    # WPay 2D settles in USD only (WPay_Gateway_Helpers::is_store_currency_supported()
    # rejects anything else) — convert CAD totals so the customer is charged the
    # USD equivalent, same pattern as pymtz above.
    if (order.currency or "").upper() == "CAD":
        wc_amount   = round(float(order.total) / USD_CONVERSION_RATE, 2)
        wc_currency = "USD"
        # Admin-facing only — order.total/currency stay the CAD cart price
        # on purpose (see models/order.py settled_currency/settled_amount).
        order.settled_currency = wc_currency
        order.settled_amount   = wc_amount
    else:
        wc_amount   = float(order.total)
        wc_currency = (order.currency or "USD").upper()

    if wc_amount < WPAY_WP_MIN_AMOUNT:
        raise HTTPException(
            400,
            f"WPay requires a minimum order total of ${WPAY_WP_MIN_AMOUNT:.2f} USD "
            f"(this order converts to ${wc_amount:.2f} USD).",
        )

    # Per WPay's compliance notice: currency must be USD — the CAD→USD
    # conversion above already guarantees this for every currency this
    # business actually uses, but this is the explicit, compliance-mapped
    # check rather than relying on that as an implicit side effect.
    if wc_currency != "USD":
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"wpay_2d declined: currency {wc_currency} is not USD"
        await db.commit()
        raise HTTPException(400, "WPay 2D only supports USD transactions.")

    bill_same = (payload.bill_same or "1") == "1"
    b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
    b_addr2   = (payload.address2 if bill_same else payload.bill_address2) or ""
    b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
    b_state   = (payload.province if bill_same else payload.bill_province) or ""
    b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
    b_country = (payload.country  if bill_same else payload.bill_country)  or "US"

    # Per WPay's compliance notice: only these billing GEOs are approved —
    # anything else is force-declined on their end regardless, so reject it
    # here before spending a WooCommerce order on it. Card scheme (VISA/
    # Mastercard only) is NOT checkable here — the plugin's own Basis Theory
    # tokenization means we never see the card brand; that restriction is
    # enforced entirely on WPay's hosted card form, not in this backend.
    _wpaywp_geos_raw = (getattr(settings, "WPAY_WP_ALLOWED_GEOS", "") or "").strip()
    _wpaywp_allowed_geos = {g.strip().upper() for g in _wpaywp_geos_raw.split(",") if g.strip()}
    if _wpaywp_allowed_geos and b_country.strip().upper() not in _wpaywp_allowed_geos:
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"wpay_2d declined: billing country '{b_country}' outside WPay's approved GEO list"
        await db.commit()
        raise HTTPException(400, f"Card payments are not currently available for billing country '{b_country}'.")

    try:
        client  = WPayWPClient()
        wc_resp = await client.create_order(
            external_order_id = order.id,
            amount      = wc_amount,
            currency    = wc_currency,
            first_name  = payload.first_name or "",
            last_name   = payload.last_name  or "",
            email       = payload.email,
            phone       = payload.phone      or "",
            address1    = b_addr1,
            address2    = b_addr2,
            city        = b_city,
            state       = b_state,
            postal_code = b_zip,
            country     = b_country,
        )
        wc_order_id = wc_resp.get("id")
        pay_url     = wc_resp["payment_url"]

        order.payment_ref   = f"wc:{wc_order_id}"
        order.payment_notes = f"wpay_2d via WC order #{wc_order_id} → {pay_url}"
        await db.commit()

        # Webhook fallback — the wpay_2d webhook has shown intermittent
        # invalid-signature failures on some deliveries; this poll catches
        # payments that complete without the webhook ever landing.
        from tasks.celery_app import check_wpay_2d_payment
        check_wpay_2d_payment.apply_async(
            args=[order.id, wc_order_id], countdown=120,
        )

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": pay_url,
            "paymentId":   f"wc:{wc_order_id}",
        }

    except WPayWPError as e:
        logger.error(f"[wpay_2d] WC order create failed for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"wpay_2d error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Could not start WPay payment: {e}")


# ─── POST /api/checkout/interac ──────────────────────────────────────────────

# ─── POST /api/checkout/onramp_wp ────────────────────────────────────────────
#
# Customer-facing endpoint for the "Card (Alt)" option — routes through the
# WordPress + 2530gateway plugin (ONRAMP_WP_ENABLED=true).

@router.post("/onramp_wp", dependencies=[Depends(_rate_limit_submission)])
async def checkout_onramp_wp(
    payload: CardCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Card payment via the WP plugin onramp. Creates the pending order, then
    creates a WC order via the plugin's WC REST API and redirects the
    customer there. Payment confirmation arrives via /webhooks/onramp_wp,
    which marks the order paid and runs the same downstream flow as pymtz
    (Shopify create + affiliate webhook).
    """
    if not bool(getattr(settings, "ONRAMP_WP_ENABLED", False)):
        raise HTTPException(503, "Onramp not enabled")

    # Block onramp for US stores. Mirrors the UI gate in main.py — even if a
    # US customer tries to call this endpoint directly, refuse the request.
    if (payload.store_country or "").upper() == "US":
        raise HTTPException(403, "Onramp is not available for US stores")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    # Reuse the `card` PaymentMethod — the customer-facing UX is still a card,
    # we disambiguate via payment_notes/payment_ref.
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    from services.onramp_wp import OnrampWPClient, OnrampWPError

    # Send the cart amount + currency through to WC as-is. WooCommerce + the
    # 2530gateway plugin handle FX themselves — pre-converting CAD→USD here
    # caused the onramp UI (Kryptonim et al.) to label the USD value as CAD,
    # undercharging the customer by ~28%.
    wc_amount   = float(order.total)
    wc_currency = (order.currency or "USD").upper()

    bill_same = (payload.bill_same or "1") == "1"
    b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
    b_addr2   = (payload.address2 if bill_same else payload.bill_address2) or ""
    b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
    b_state   = (payload.province if bill_same else payload.bill_province) or ""
    b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
    b_country = (payload.country  if bill_same else payload.bill_country)  or "CA"

    try:
        client  = OnrampWPClient()
        wc_resp = await client.create_order(
            external_order_id = order.id,
            amount      = wc_amount,
            currency    = wc_currency,
            first_name  = payload.first_name or "",
            last_name   = payload.last_name  or "",
            email       = payload.email,
            phone       = payload.phone      or "",
            address1    = b_addr1,
            address2    = b_addr2,
            city        = b_city,
            state       = b_state,
            postal_code = b_zip,
            country     = b_country,
        )
        wc_order_id = wc_resp.get("id")
        pay_url     = wc_resp["payment_url"]

        order.payment_ref   = f"wc:{wc_order_id}"
        order.payment_notes = f"onramp_wp via WC order #{wc_order_id} → {pay_url}"
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": pay_url,
            "paymentId":   f"wc:{wc_order_id}",
        }

    except OnrampWPError as e:
        logger.error(f"[onramp_wp] order create failed for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"onramp_wp error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Onramp payment setup failed: {e}")


@router.post("/interac", dependencies=[Depends(_rate_limit_submission)])
async def checkout_interac(
    payload: InteracCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = float(brand.interac_discount if brand else 10.0)

    order = await _create_base_order(db, payload, PaymentMethod.interac, brand, discount_pct, request)

    # Reuse existing InteracPayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(InteracPayment).where(InteracPayment.order_id == order.id)
    )
    ip = existing.scalar_one_or_none()
    is_new_order = ip is None
    if ip:
        if ip.status != "matched":
            ip.expected_amount = order.total
            ip.status          = "waiting"
    else:
        db.add(InteracPayment(
            order_id        = order.id,
            expected_amount = order.total,
            status          = "waiting",
        ))

    await db.commit()

    # "Order received" email — only on first placement, not on resubmission.
    # Never blocks the checkout response — a failed send shouldn't fail the order.
    if is_new_order:
        try:
            from services.email import send_order_received_email
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            await send_order_received_email(order, accent)
        except Exception as e:
            logger.error(f"Order-received email failed for {order.id}: {e}")

    interac_email = (
        brand.interac_email if brand and brand.interac_email
        else settings.INTERAC_DEFAULT_EMAIL
    )

    return {
        "success":      True,
        "orderId":      order.id,
        "total":        float(order.total),
        "currency":     order.currency,
        "interacEmail": interac_email,
        "discountPct":  discount_pct,
        "instructions": (
            f"Send ${float(order.total):.2f} {order.currency} via Interac e-Transfer to "
            f"{interac_email}. In the message/note field, enter your Order ID: {order.id}"
        ),
    }


# ─── POST /api/checkout/zelle ────────────────────────────────────────────────

class ZelleCheckoutRequest(CheckoutBase):
    pass


@router.post("/zelle", dependencies=[Depends(_rate_limit_submission)])
async def checkout_zelle(
    payload: ZelleCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    US equivalent of Interac. Customer sends Zelle manually to our US email.
    Admin matches the payment manually via the admin dashboard.
    """
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    # Zelle discount — same 10% as Interac
    discount_pct = float(getattr(brand, "zelle_discount", None) or 10.0)

    order = await _create_base_order(db, payload, PaymentMethod.zelle, brand, discount_pct, request)

    # Reuse existing ZellePayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(ZellePayment).where(ZellePayment.order_id == order.id)
    )
    zp = existing.scalar_one_or_none()
    is_new_order = zp is None
    if zp:
        if zp.status != "matched":
            zp.expected_amount = order.total
            zp.status          = "waiting"
    else:
        db.add(ZellePayment(
            order_id        = order.id,
            expected_amount = order.total,
            status          = "waiting",
        ))

    await db.commit()

    # "Order received" email — only on first placement, not on resubmission
    # (page refresh / customer editing the discount code re-hits this same
    # endpoint against the same order). Never blocks the checkout response —
    # a failed send here shouldn't fail the order itself.
    if is_new_order:
        try:
            from services.email import send_order_received_email
            accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
            await send_order_received_email(order, accent)
        except Exception as e:
            logger.error(f"Order-received email failed for {order.id}: {e}")

    zelle_email = settings.ZELLE_DEFAULT_EMAIL or ""

    return {
        "success":     True,
        "orderId":     order.id,
        "total":       float(order.total),
        "currency":    order.currency,
        "zelleEmail":  zelle_email,
        "discountPct": discount_pct,
        "instructions": (
            f"Send ${float(order.total):.2f} {order.currency} via Zelle to "
            f"{zelle_email}. In the memo/note field, enter your Order ID: {order.id}"
        ),
    }


# ─── POST /api/checkout/crypto ───────────────────────────────────────────────

@router.post("/crypto", dependencies=[Depends(_rate_limit_submission)])
async def checkout_crypto(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = float(brand.crypto_discount if brand else 10.0)

    order = await _create_base_order(db, payload, PaymentMethod.crypto, brand, discount_pct, request)

    # Use brand-specific BTCPay store if configured
    btcpay_store = (brand.btcpay_store_id if brand and brand.btcpay_store_id else None)
    client       = BTCPayClient(store_id=btcpay_store)

    webhook_url = f"{settings.BASE_URL}/webhooks/btcpay"

    try:
        invoice = await client.create_invoice(
            order_id       = order.id,
            amount         = float(order.total),
            currency       = order.currency,
            customer_email = payload.email,
            customer_name  = f"{payload.first_name or ''} {payload.last_name}".strip(),
            webhook_url    = webhook_url,
        )

        btcpay_id  = invoice["id"]
        invoice_url = invoice.get("checkoutLink", "")

        # Reuse existing CryptoInvoice if present (customer re-submitted); else create
        existing = await db.execute(
            select(CryptoInvoice).where(CryptoInvoice.order_id == order.id)
        )
        ci = existing.scalar_one_or_none()
        if ci:
            ci.btcpay_invoice_id  = btcpay_id
            ci.btcpay_invoice_url = invoice_url
            ci.amount_fiat        = order.total
            ci.status             = "New"
        else:
            db.add(CryptoInvoice(
                order_id           = order.id,
                btcpay_invoice_id  = btcpay_id,
                btcpay_invoice_url = invoice_url,
                amount_fiat        = order.total,
                status             = "New",
            ))

        order.payment_ref = btcpay_id
        await db.commit()

        # Kick off background polling as webhook fallback
        from tasks.celery_app import check_btcpay_invoice
        check_btcpay_invoice.apply_async(
            args=[order.id, btcpay_id],
            countdown=120,   # start checking after 2 minutes
        )

        return {
            "success":          True,
            "orderId":          order.id,
            "btcpayInvoiceUrl": invoice_url,
            "discountPct":      discount_pct,
            "total":            float(order.total),
            "currency":         order.currency,
        }

    except BTCPayError as e:
        logger.exception(f"BTCPay invoice creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Crypto payment unavailable: {e}")
    
    
# ─── POST /api/checkout/altcoin ───────────────────────────────────────────────

@router.post("/altcoin", dependencies=[Depends(_rate_limit_submission)])
async def checkout_altcoin(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = 7.0  # NowPayments altcoin discount fixed at 7%

    order = await _create_base_order(db, payload, PaymentMethod.altcoin, brand, discount_pct, request)

    client      = NowPaymentsClient()
    ipn_url     = f"{settings.BASE_URL}/webhooks/nowpayments"
    # Mirror v2 + country onto the post-payment confirmation page if the
    # customer started on the v2 checkout. NOWPayments does its own redirect
    # after invoice settles, so the flag must be baked into the URL now.
    success_url = f"{settings.BASE_URL}/order/{order.id}/confirmation{_v2_referer_suffix(request)}"
    cancel_url  = f"{settings.BASE_URL}/"

    try:
        invoice = await client.create_invoice(
            order_id         = order.id,
            amount           = float(order.total),
            currency         = order.currency,
            ipn_callback_url = ipn_url,
            success_url      = success_url,
            cancel_url       = cancel_url,
        )

        np_invoice_id = str(invoice["id"])
        invoice_url   = invoice.get("invoice_url", "")

        db.add(NowPaymentsInvoice(
            order_id      = order.id,
            np_invoice_id = np_invoice_id,
            invoice_url   = invoice_url,
            amount_fiat   = order.total,
            status        = "waiting",
        ))
        order.payment_ref = np_invoice_id
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "invoiceUrl":  invoice_url,
            "discountPct": discount_pct,
            "total":       float(order.total),
            "currency":    order.currency,
        }

    except NowPaymentsError as e:
        logger.exception(f"NowPayments invoice creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Altcoin payment unavailable: {e}")


# ─── GET /api/checkout/pymtz-verify/{order_id} ───────────────────────────────
# Called by confirmation.html when the customer returns from pymtz's hosted
# payment page. pymtz has no webhooks, so we poll their API here to learn
# the real outcome, mark the order paid if confirmed, and fire all side
# effects (MPC Shopify order, affiliate log, Resend email).
#
# Idempotent — safe to call multiple times. If the order is already paid/
# failed/expired we return immediately without hitting pymtz's API again.

@router.get("/pymtz-verify/{order_id}")
async def pymtz_verify(
    order_id: str,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    from services.pymtz import PymtzClient, PymtzError, PYMTZ_STATUS_MAP
    from datetime import datetime, timezone
    import httpx as _httpx

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    # Already resolved — return current status without touching pymtz API
    if order.payment_status != PaymentStatus.pending:
        return {
            "orderId":       order.id,
            "paymentStatus": order.payment_status.value,
        }

    if not order.payment_ref:
        return {"orderId": order.id, "paymentStatus": "pending"}

    # ── Ask pymtz for current payment status ──────────────────────────────────
    # Use the same per-country account the payment was created on, otherwise the
    # GET hits the wrong merchant and returns 404 / 401.
    pymtz_country = "US" if (order.currency or "").upper() == "USD" else "CA"
    try:
        payment = await PymtzClient(country=pymtz_country).get_payment(order.payment_ref)
    except Exception as e:
        logger.warning(f"[pymtz-verify] get_payment failed for {order.id}: {e}")
        return {"orderId": order.id, "paymentStatus": "pending"}

    pymtz_status = payment.get("status", "")
    our_status   = PYMTZ_STATUS_MAP.get(pymtz_status)

    if not our_status or our_status == "pending":
        return {"orderId": order.id, "paymentStatus": "pending"}

    # ── Update order ──────────────────────────────────────────────────────────
    order.payment_status = PaymentStatus(our_status)
    if our_status == "paid":
        order.paid_at       = datetime.now(timezone.utc)
        order.payment_notes = f"pymtz {order.payment_ref} confirmed via return-url verify."
    elif our_status == "failed":
        order.payment_notes = f"pymtz {order.payment_ref} failed (verify check)."
    elif our_status == "expired":
        order.payment_notes = f"pymtz {order.payment_ref} expired (verify check)."
    await db.commit()

    logger.info(f"[pymtz-verify] Order {order.id} → {our_status} (pymtz status: {pymtz_status})")

    # ── Side effects — only on paid ───────────────────────────────────────────
    if our_status == "paid":
        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as db2:
            res2 = await db2.execute(
                select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items))
            )
            order2 = res2.scalar_one_or_none()
            if order2:
                from services.order_finalize import finalize_paid_order
                await finalize_paid_order(order2, db2, label="pymtz-verify")

    return {"orderId": order.id, "paymentStatus": our_status}


# ─── GET /api/checkout/status/{order_id} ──────────────────────────────────────

@router.get("/status/{order_id}")
async def order_status(order_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order  = result.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    return {
        "orderId":       order.id,
        "storeName":     order.store_name,
        "paymentMethod": order.payment_method.value if hasattr(order.payment_method, 'value') else str(order.payment_method),
        "paymentStatus": order.payment_status.value if hasattr(order.payment_status, 'value') else str(order.payment_status),
        "total":         float(order.total),
        "currency":      order.currency,
        "createdAt":     order.created_at.isoformat() if order.created_at else None,
        "paidAt":        order.paid_at.isoformat() if order.paid_at else None,
    }