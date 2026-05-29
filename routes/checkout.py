"""
POST /api/checkout/card      → Helcim credit card
POST /api/checkout/interac   → Interac e-Transfer (create pending order)
POST /api/checkout/crypto    → BTCPay Server invoice
GET  /api/checkout/status/{order_id}
"""
import logging
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
from services.lasso import LassoClient, LassoError
from services.whop import WhopClient, WhopError
from utils.cloaking import cloak_items, cloak_items_lasso, build_lasso_cart

router  = APIRouter(prefix="/api/checkout", tags=["checkout"])
logger  = logging.getLogger(__name__)


# ─── Shared input schemas ─────────────────────────────────────────────────────

class CartItem(BaseModel):
    product_id: str | None = Field(None, max_length=50)
    title:      str        = Field(..., min_length=1, max_length=500)
    variant:    str | None = Field(None, max_length=200)
    qty:        int        = Field(1, ge=1, le=100)
    price:      float      = Field(..., ge=0, le=10000)


class CheckoutBase(BaseModel):
    # Optional pre-reserved order ID (from /api/checkout/reserve on page load)
    order_id: str | None = None

    # Contact
    email:      str
    first_name: str | None = None
    last_name:  str

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
    store_country: str = "CA"   # "CA" or "US" — which store the order came from (not shipping)

    # Discount info — applies to all payment methods
    discount_code: str | None = None
    discount_amount: float = 0.0
    payment_method_discount: float = 0.0


class CardCheckoutRequest(CheckoutBase):
    helcim_pay_token: str | None = None


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


def _validate_cart(items: list, claimed_subtotal: float) -> None:
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

    if abs(computed - round(float(claimed_subtotal), 2)) > 0.01:
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
    store_name = brand.store_name if brand else "Checkout"
    discount_amount, total = _compute_total(data.subtotal, discount_pct)

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
            ))

        await db.flush()
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
        ))

    await db.flush()
    return order


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
    _validate_cart(payload.items, payload.subtotal)
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
            # Whop is a card-variant (cloaked) — gets stored as PaymentMethod.card
            # in the DB but is distinguished by payment_ref starting with "ch_".
            v_normalized = "card" if v == "whop" else v
            try:
                new_method = PaymentMethod(v_normalized)
            except (ValueError, KeyError):
                return {"ok": False, "error": "invalid_payment_method"}

            # Look up the brand for discount percentages (default fallbacks if not set)
            brand_result = await db.execute(
                select(Brand).where(Brand.id == order.brand_id)
            )
            brand = brand_result.scalar_one_or_none()

            if new_method in (PaymentMethod.interac, PaymentMethod.zelle):
                pct = float(brand.interac_discount) if brand and brand.interac_discount else 5.0
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

@router.post("/card")
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
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    description = f"Order {order.id}"
    if payload.items:
        first = payload.items[0].title
        extra = len(payload.items) - 1
        description = first + (f" +{extra} more" if extra > 0 else "")

    return_url = f"{settings.BASE_URL}/order/{order.id}/confirmation"
    cancel_url = f"{settings.BASE_URL}/"

    try:
        client  = PymtzClient()
        payment = await client.create_payment(
            order_id    = order.id,
            amount      = float(order.total),
            currency    = order.currency,
            description = description,
            email       = payload.email,
            return_url  = return_url,
            cancel_url  = cancel_url,
            metadata    = {
                "source_domain": payload.source_domain or "",
                "store_name":    order.store_name or "",
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


# ─── POST /api/checkout/interac ──────────────────────────────────────────────

@router.post("/interac")
async def checkout_interac(
    payload: InteracCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)
    discount_pct = float(brand.interac_discount if brand else 5.0)

    order = await _create_base_order(db, payload, PaymentMethod.interac, brand, discount_pct, request)

    # Reuse existing InteracPayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(InteracPayment).where(InteracPayment.order_id == order.id)
    )
    ip = existing.scalar_one_or_none()
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


@router.post("/zelle")
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
    _validate_cart(payload.items, payload.subtotal)
    # Same 5% default discount as Interac — both are manual bank transfers
    discount_pct = float(brand.interac_discount if brand else 5.0)

    order = await _create_base_order(db, payload, PaymentMethod.zelle, brand, discount_pct, request)

    # Reuse existing ZellePayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(ZellePayment).where(ZellePayment.order_id == order.id)
    )
    zp = existing.scalar_one_or_none()
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

@router.post("/crypto")
async def checkout_crypto(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)
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

@router.post("/altcoin")
async def checkout_altcoin(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)
    discount_pct = 7.0  # NowPayments altcoin discount fixed at 7%

    order = await _create_base_order(db, payload, PaymentMethod.altcoin, brand, discount_pct, request)

    client      = NowPaymentsClient()
    ipn_url     = f"{settings.BASE_URL}/webhooks/nowpayments"
    success_url = f"{settings.BASE_URL}/order/{order.id}/confirmation"
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


# ─── POST /api/checkout/lasso ────────────────────────────────────────────────
# Cloaked CC checkout via Lasso → Whop payment rails.
#
# Flow:
#   1. Create order in our DB with REAL product titles (for fulfillment)
#   2. Cloak all item titles → universal decoy before calling Lasso
#   3. POST cloaked cart to Lasso API → get session_id
#   4. Return redirect URL to frontend → customer lands on Lasso checkout
#   5. Whop fires /webhooks/whop on payment completion → marks order paid

class LassoCheckoutRequest(CheckoutBase):
    pass


@router.post("/lasso")
async def checkout_lasso(
    payload: LassoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)

    # 1. Create order with REAL item titles — our DB always has the truth
    order = await _create_base_order(
        db, payload, PaymentMethod.card, brand, 0.0, request
    )
    await db.commit()

    # 2. Cloak items — Lasso-specific mapping (peptide → dedicated decoy)
    cloaked     = cloak_items_lasso(payload.items)
    lasso_cart  = build_lasso_cart(cloaked)

    # 3. Create Lasso session
    try:
        client     = LassoClient()
        session_id = await client.create_session(
            cart      = lasso_cart,
            currency  = payload.currency,
            country   = payload.store_country,
            order_id  = order.id,
        )
        redirect_url = client.build_redirect_url(session_id)

        order.payment_ref   = session_id
        order.payment_notes = f"lasso session {session_id}"
        await db.commit()

        logger.info(f"[Lasso] Order {order.id} → session {session_id}")

        return {
            "success":     True,
            "orderId":     order.id,
            "sessionId":   session_id,
            "redirectUrl": redirect_url,
        }

    except LassoError as e:
        logger.exception(f"[Lasso] Session creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = str(e)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")


# ─── POST /api/checkout/whop-embed ───────────────────────────────────────────
# Cloaked CC checkout via Whop's embedded checkout widget — direct integration,
# no Lasso, no bridge worker. This is PARALLEL to /api/checkout/card and /lasso:
# customers see "Card (WHOP)" as a separate payment option on the frontend.
#
# Flow:
#   1. Create order in our DB with REAL product titles (for fulfillment)
#   2. Call Whop API /checkout_configurations → creates a one-time plan at the
#      customer's actual cart total. Plan title is the cloaked decoy name.
#      metadata.order_id = our portal order_id so the webhook can match back.
#   3. Return { plan_id, session_id, whop_email } to the frontend, which mounts
#      <div data-whop-checkout-plan-id=... data-whop-checkout-session=...>
#      so the customer pays inside an iframe (PCI stays on whop.com).
#   4. Whop fires /webhooks/whop on payment completion → marks order paid,
#      auto-creates Shopify fulfillment order, sends affiliate webhook.

class WhopEmbedCheckoutRequest(CheckoutBase):
    # email / last_name made optional because the frontend creates the Whop
    # session AS SOON as the customer picks "Card (WHOP)" — before they've
    # filled the form. Real values are synced into the iframe later via
    # wco.setEmail / wco.setAddress (and into our DB via autosave). Whop
    # doesn't require email at session creation; it's collected inside the
    # iframe (which we hide and populate programmatically).
    email:     str = ""
    last_name: str = ""


@router.post("/whop-embed")
async def checkout_whop_embed(
    payload: WhopEmbedCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal)

    # ── Master kill-switch ─────────────────────────────────────────────────
    # WHOP_ENABLED=false in .env disables this endpoint without touching keys
    # or limits. Also hides the frontend option (checked in main.py).
    if not bool(getattr(settings, "WHOP_ENABLED", True)):
        logger.info("[Whop] WHOP_ENABLED=false — refusing whop-embed request")
        return {
            "success":  False,
            "fallback": True,
            "reason":   "whop_disabled",
            "detail": (
                "This payment option is temporarily unavailable. "
                "Please choose Credit Card, Interac, or Crypto."
            ),
        }

    # ── Daily volume cap on Whop ────────────────────────────────────────────
    # Sum today's UTC-day card orders that were routed through Whop (their
    # payment_ref starts with the Whop session prefix "ch_"). We only refuse
    # NEW orders once today's running total has ALREADY reached the cap —
    # an order that would tip us over is still allowed through. So with a
    # $300 cap and $0 used today, a $500 order goes through (becomes $500
    # used); the NEXT order is rejected because we're already over.
    # Self-imposed throttle to keep Whop volume predictable and reduce
    # compliance review surface.
    daily_limit = float(getattr(settings, "WHOP_DAILY_LIMIT", 0) or 0)
    if daily_limit > 0:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await db.execute(
            select(func.coalesce(func.sum(Order.total), 0))
            .where(Order.created_at >= today_start)
            .where(Order.payment_method == PaymentMethod.card)
            .where(Order.payment_ref.like("ch_%"))
        )
        today_total = float(result.scalar() or 0)
        new_order_total = float(payload.subtotal)

        # Reject only if we're ALREADY at/over the cap. Lets the first order
        # of the day go through even if it's larger than the cap.
        if today_total >= daily_limit:
            logger.warning(
                f"[Whop] Daily limit reached: today={today_total:.2f} >= "
                f"limit={daily_limit:.2f} — refusing whop-embed for {payload.email} "
                f"(would-be new order: {new_order_total:.2f})"
            )
            return {
                "success":  False,
                "fallback": True,
                "reason":   "daily_limit_reached",
                "detail": (
                    "This payment option is temporarily at capacity. "
                    "Please choose Credit Card, Interac, or Crypto."
                ),
                "today_total":  round(today_total, 2),
                "daily_limit":  daily_limit,
            }
        elif (today_total + new_order_total) > daily_limit:
            # Allowed but we're about to tip over — log it so you know.
            logger.info(
                f"[Whop] This order will push past daily limit: "
                f"today={today_total:.2f} + new={new_order_total:.2f} > limit={daily_limit:.2f}. "
                f"Allowing (last order of the day on Whop)."
            )

    # 1. Create order with REAL item titles in our DB — single source of truth
    order = await _create_base_order(
        db, payload, PaymentMethod.card, brand, 0.0, request
    )
    await db.commit()

    # 2. Build cloaked items (informational — we don't pass them to Whop because
    #    Whop only sees a single inline plan, but cloaking the title is what
    #    keeps peptide names off Whop's records).
    _ = cloak_items(payload.items)  # noqa

    # 3. Create a Whop checkout configuration at the order's actual total.
    #    Deliberately omit return_url (BASE_URL would leak in Whop's records)
    #    and don't pass identifying metadata (source_domain / store_name).
    #    The frontend uses skip-redirect + onCheckoutComplete callback to
    #    redirect to /order/{id}/confirmation locally.
    return_url = settings.WHOP_RETURN_URL or None

    try:
        client  = WhopClient()
        session = await client.create_checkout_session(
            order_id   = order.id,
            amount     = float(order.total),
            email      = payload.email,
            currency   = order.currency,
            return_url = return_url,
            extra_meta = None,
        )
    except WhopError as e:
        logger.exception(f"[Whop-embed] Whop session creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"whop-embed failed: {e}"
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")

    # If a tier plan was used, the actual amount Whop will charge is the tier
    # price, NOT the original cart total. Reconcile order.total so our DB
    # records the true charged amount (matches what shows on customer's
    # statement and our confirmation page). Track the original cart subtotal
    # in payment_notes for audit / reconciliation.
    charged_amount = float(session.get("charged_amount", session["amount"]))
    original_total = float(order.total)
    tier_was_used  = bool(session.get("tier_used", False))

    order.payment_ref = session["session_id"]
    if tier_was_used and abs(charged_amount - original_total) > 0.005:
        # Update both subtotal and total so they stay consistent in the DB
        order.total    = Decimal(str(charged_amount))
        order.subtotal = Decimal(str(charged_amount))
        order.payment_notes = (
            f"Whop embedded → session {session['session_id']} (tier "
            f"plan {session['plan_id']}). Cart was ${original_total:.2f}, "
            f"customer charged ${charged_amount:.2f} (tier match)."
        )
        logger.info(
            f"[Whop-embed] Tier reconciliation: order {order.id} "
            f"cart=${original_total:.2f} → charged=${charged_amount:.2f}"
        )
    else:
        order.payment_notes = (
            f"Whop embedded checkout → session {session['session_id']} "
            f"(plan {session['plan_id']})"
        )
    await db.commit()

    return {
        "success":         True,
        "orderId":         order.id,
        "whop_hosted":     True,
        "purchase_url":    session["purchase_url"],
        "session_id":      session["session_id"],
        "plan_id":         session["plan_id"],
        "amount":          session["amount"],          # original cart amount
        "charged_amount":  charged_amount,             # what Whop will actually charge
        "tier_used":       tier_was_used,
        "currency":        session["currency"],
        "sandbox":         session.get("sandbox", False),
        "whop_email":      session.get("whop_email", ""),
    }


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
    try:
        payment = await PymtzClient().get_payment(order.payment_ref)
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
                # 1. MPC Shopify order
                shopify_order_number = None
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order2)
                    if shopify_order:
                        shopify_order_number = str(shopify_order.get("order_number", ""))
                        logger.info(
                            f"✅ Shopify order #{shopify_order_number} "
                            f"auto-created for {order2.id} (pymtz verify)"
                        )
                    else:
                        logger.error(f"Shopify auto-create returned None for {order2.id} (pymtz verify)")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order2.id} (pymtz verify): {e}")

                # 2. Affiliate log
                try:
                    affiliate_url = getattr(settings, "AFFILIATE_DASHBOARD_URL", "")
                    if affiliate_url and order2.discount_code:
                        items_summary = ", ".join(
                            f"{item.qty}x {item.title}" for item in (order2.items or [])
                        )
                        async with _httpx.AsyncClient(timeout=10.0) as hc:
                            await hc.post(
                                f"{affiliate_url}/api/webhooks/order-paid",
                                json={
                                    "customer_first_name": order2.first_name or order2.last_name or "Customer",
                                    "items_summary":       items_summary,
                                    "order_total":         float(order2.total),
                                    "discount_code":       (order2.discount_code or "").upper(),
                                    "source":              "portal",
                                    "external_order_id":   order2.id,
                                    "source_store":        order2.source_domain or order2.store_name or "",
                                    "currency":            order2.currency or "CAD",
                                },
                            )
                        logger.info(f"Affiliate webhook sent for {order2.id} (pymtz verify)")
                except Exception as e:
                    logger.warning(f"Affiliate webhook failed for {order2.id} (pymtz verify): {e}")

                # 3. Resend confirmation email
                if order2.email:
                    try:
                        from models.brand import Brand
                        brand_res = await db2.execute(
                            select(Brand).where(Brand.id == order2.brand_id)
                        )
                        brand  = brand_res.scalar_one_or_none()
                        accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
                        from services.email import send_confirmation_email
                        await send_confirmation_email(
                            order2,
                            shopify_order_number=shopify_order_number,
                            accent=accent,
                        )
                        logger.info(
                            f"✉️  Confirmation email sent for {order2.id} "
                            f"(pymtz verify) → {order2.email}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Confirmation email failed for {order2.id} (pymtz verify): {e}"
                        )

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