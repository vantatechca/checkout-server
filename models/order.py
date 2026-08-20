"""
Order models — Merged version
==============================

This is the canonical order.py with all features:

  • Promo code discount (e.g. HANNAH10 = 10% off) — separate fields:
      promo_discount_pct, promo_discount_amount

  • Payment-method discount (Interac/Zelle 5%, Crypto 10%) — kept on:
      discount_pct, discount_amount

  • Original (pre-discount) prices tracked on Order + OrderItem
      so the UI can show "Was $50, now $45" comparisons

  • Underpayment detection on Interac/Zelle:
      received_amount column + new "underpaid" status

  • Customer email tracking:
      last_customer_email_at, customer_emails_sent on Order
      CustomerEmailLog audit table

⚠️  Requires DB migration before this code can run. See migration script.
"""
import enum
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime,
    Enum, ForeignKey, Index, Boolean
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


def _classify_device(ua: str | None) -> str:
    """Rough device class from a User-Agent string: Mobile / Tablet / Desktop."""
    if not ua:
        return "Unknown"
    s = ua.lower()
    if "ipad" in s or "tablet" in s or ("android" in s and "mobile" not in s):
        return "Tablet"
    if any(k in s for k in ("mobi", "iphone", "ipod", "android", "windows phone", "blackberry")):
        return "Mobile"
    return "Desktop"


def _classify_processor(method: str | None, ref: str | None, notes: str | None) -> str | None:
    """
    Sub-classify a `card` payment by the actual processor that handled it.
    Multiple processors all use PaymentMethod.card — this disambiguates them
    for the admin dashboard so each shows a distinct badge.

    Sniffs `payment_ref` first (cheapest), then falls back to keywords in
    `payment_notes`. Returns a short identifier like "pymtz", "transak",
    "moonpay", "onramp_wp", "helcim", "stripe", or None if unknown.
    """
    if method == "shopify":
        return "shopify"
    if method != "card":
        return None   # interac/zelle/crypto/altcoin are self-describing

    r = (ref   or "").lower()
    n = (notes or "").lower()

    # Highriskify direct API (Transak/MoonPay/Topper picker) — payment_notes
    # carries the specific provider when the wallet was created.
    if r.startswith("hr:") or "highriskify" in n:
        if "via moonpay" in n:
            return "moonpay"
        if "via transak" in n:
            return "transak"
        if "via topper" in n:
            return "topper"
        return "onramp"           # provider unknown / not yet picked
    # Legacy WP-plugin onramp path
    if r.startswith("wc:") or "onramp_wp" in n:
        return "onramp_wp"
    # pymtz card processor
    if r.startswith("pay_") or "pymtz" in n:
        return "pymtz"
    # Authorize.net (TSYS, SWISSCO merchant, MCC 7299 cloak)
    if r.startswith("an:") or "authnet" in n or "authorize.net" in n:
        return "authnet"
    # Helcim hosted checkout
    if r.startswith("helcim_") or "helcim" in n:
        return "helcim"
    # Stripe checkout session / payment intent
    if r.startswith("cs_") or r.startswith("pi_") or r.startswith("seti_") or "stripe" in n:
        return "stripe"
    return None


class PaymentMethod(str, enum.Enum):
    card    = "card"
    interac = "interac"
    crypto  = "crypto"
    zelle   = "zelle"
    altcoin = "altcoin"
    wpay    = "wpay"
    wpay_2d = "wpay_2d"


class PaymentStatus(str, enum.Enum):
    pending   = "pending"
    paid      = "paid"
    failed    = "failed"
    refunded  = "refunded"
    expired   = "expired"
    manual    = "manual"      # Interac/Zelle matched manually by admin
    cancelled = "cancelled"


class Order(Base):
    __tablename__ = "orders"

    # --- Identity ---
    id          = Column(String(20), primary_key=True)    # ORD-XXXXXXXX
    brand_id    = Column(Integer, ForeignKey("brands.id"), nullable=False)
    store_name  = Column(String(255), nullable=False)     # denormalized

    # --- Customer ---
    email       = Column(String(255), nullable=False, index=True)
    first_name  = Column(String(100), nullable=True)
    last_name   = Column(String(100), nullable=False)
    phone       = Column(String(50), nullable=True)

    # --- Shipping address ---
    address1    = Column(String(255), nullable=True)
    address2    = Column(String(255), nullable=True)
    city        = Column(String(100), nullable=True)
    province    = Column(String(100), nullable=True)
    postal_code = Column(String(20), nullable=True)
    country     = Column(String(2), default="CA")

    # --- Billing (if different from shipping) ---
    bill_same       = Column(String(1), default="1")
    bill_address1   = Column(String(255), nullable=True)
    bill_address2   = Column(String(255), nullable=True)
    bill_city       = Column(String(100), nullable=True)
    bill_province   = Column(String(100), nullable=True)
    bill_postal     = Column(String(20), nullable=True)
    bill_country    = Column(String(2), nullable=True)

    # --- Financials ---
    subtotal          = Column(Numeric(10, 2), nullable=False)        # post-promo subtotal
    original_subtotal = Column(Numeric(10, 2), nullable=True)         # pre-promo subtotal

    # --- Promo code discount (e.g. HANNAH10 = 10% off, TJWIN = $5 off) ---
    discount_code         = Column(String(100), nullable=True)
    promo_discount_pct    = Column(Numeric(5, 2), default=0)
    promo_discount_amount = Column(Numeric(10, 2), default=0)

    # --- Payment-method discount (Interac/Zelle 5%, Crypto 10%) ---
    # NOTE: these columns existed before; they're now repurposed as the
    # method discount. Promo code discount has its own separate columns above.
    discount_pct    = Column(Numeric(5, 2), default=0)     # method discount %
    discount_amount = Column(Numeric(10, 2), default=0)    # method discount $

    total           = Column(Numeric(10, 2), nullable=False)
    currency        = Column(String(3), default="CAD")

    # --- Settlement currency (when different from the cart currency above) ---
    # pymtz, WPay HPP, and WPay 2D are USD-only processors: a CAD cart gets
    # converted to USD before the charge, but `total`/`currency` above stay
    # the CAD cart price on purpose — Shopify order creation and affiliate
    # commission payouts both read `total` directly, and changing it would
    # silently alter Shopify's own accounting and reduce affiliate payouts
    # for these orders. These two columns capture what was ACTUALLY charged,
    # for admin-facing display only. NULL means "no conversion happened —
    # `total`/`currency` above already reflect the real charge."
    settled_currency = Column(String(3), nullable=True)
    settled_amount   = Column(Numeric(10, 2), nullable=True)

    # --- Payment ---
    payment_method  = Column(Enum(PaymentMethod), nullable=False, index=True)
    payment_status  = Column(Enum(PaymentStatus), default=PaymentStatus.pending, index=True)
    payment_ref     = Column(String(255), nullable=True, index=True)  # Helcim/Stripe txn ID, BTCPay invoice ID, etc.
    payment_notes   = Column(Text, nullable=True)
    paid_at         = Column(DateTime, nullable=True)

    # --- Fulfillment (Shippo shipping label) ---
    tracking_number      = Column(String(64), nullable=True)
    tracking_url         = Column(Text, nullable=True)   # carrier tracking URLs can exceed 255 chars
    carrier               = Column(String(32), nullable=True)   # e.g. "usps", "ups", "canada_post"
    label_url             = Column(Text, nullable=True)  # Shippo-hosted PDF/PNG — signed URLs routinely exceed 255 chars
    shippo_transaction_id = Column(String(64), nullable=True)
    shipped_at             = Column(DateTime, nullable=True)     # set when the label is purchased
    package_weight_oz      = Column(Numeric(6, 2), nullable=True)
    package_length_in      = Column(Numeric(5, 2), nullable=True)
    package_width_in       = Column(Numeric(5, 2), nullable=True)
    package_height_in      = Column(Numeric(5, 2), nullable=True)
    # True only when marked paid AND labeled through the combined Shippo
    # flow (Pending tab's "Mark Paid (Shippo)" button) — distinguishes that
    # from a Shopify-paid order that separately got a label bought for it,
    # so the Shipping tab can show exactly the former.
    paid_via_shippo        = Column(Boolean, nullable=False, default=False)

    # --- Customer email tracking ---
    last_customer_email_at = Column(DateTime, nullable=True)
    customer_emails_sent   = Column(Integer, default=0)

    # --- Business info (currently princetonpeptides.com only) ---
    # Optional research-vertical context collected on Princeton's checkout.
    # NULL for stores that don't render the Research Information section.
    company         = Column(String(255), nullable=True)
    research_field  = Column(String(100), nullable=True)

    # --- Metadata ---
    ip_address      = Column(String(45), nullable=True)
    user_agent      = Column(Text, nullable=True)
    source_domain   = Column(String(255), nullable=True)   # which checkout domain was used

    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # --- Relationships ---
    items           = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    interac_payment = relationship("InteracPayment", back_populates="order", uselist=False)
    zelle_payment   = relationship("ZellePayment", back_populates="order", uselist=False)
    crypto_invoice  = relationship("CryptoInvoice", back_populates="order", uselist=False)
    nowpayments_invoice = relationship("NowPaymentsInvoice", back_populates="order", uselist=False)

    __table_args__ = (
        Index("idx_brand_status", "brand_id", "payment_status"),
        Index("idx_email_brand", "email", "brand_id"),
        Index("idx_created", "created_at"),
        Index("idx_status_paidat", "payment_status", "paid_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "storeName":     self.store_name,
            "sourceDomain":  self.source_domain,
            "device":        _classify_device(self.user_agent),
            "email":         self.email,
            "firstName":     self.first_name,
            "lastName":      self.last_name,
            "phone":         self.phone,
            "address1":      self.address1,
            "address2":      self.address2,
            "city":          self.city,
            "province":      self.province,
            "postalCode":    self.postal_code,
            "country":       self.country,

            # Financial breakdown
            "subtotal":             float(self.subtotal),
            "originalSubtotal":     float(self.original_subtotal or self.subtotal or 0),
            "discountCode":         self.discount_code,
            "promoDiscountPct":     float(self.promo_discount_pct or 0),
            "promoDiscountAmount":  float(self.promo_discount_amount or 0),
            "methodDiscountPct":    float(self.discount_pct or 0),
            "methodDiscountAmount": float(self.discount_amount or 0),
            "discountPct":          float(self.discount_pct or 0),  # legacy alias
            "total":                float(self.total),
            "currency":             self.currency,
            # What was ACTUALLY charged, when different from the cart price
            # above (pymtz/WPay HPP/WPay 2D convert CAD carts to USD before
            # charging). None when no conversion happened — total/currency
            # above already reflect the real charge in that case.
            "settledCurrency":      self.settled_currency,
            "settledAmount":        float(self.settled_amount) if self.settled_amount is not None else None,

            # Payment
            "paymentMethod":        self.payment_method,
            "paymentProcessor":     _classify_processor(self.payment_method, self.payment_ref, self.payment_notes),
            "paymentStatus":        self.payment_status,
            "paidAt":               self.paid_at.isoformat() if self.paid_at else None,
            "createdAt":            self.created_at.isoformat() if self.created_at else None,

            # Fulfillment (Shippo shipping label)
            "trackingNumber":       self.tracking_number,
            "trackingUrl":          self.tracking_url,
            "carrier":              self.carrier,
            "labelUrl":             self.label_url,
            "shippedAt":            self.shipped_at.isoformat() if self.shipped_at else None,
            "packageWeightOz":      float(self.package_weight_oz) if self.package_weight_oz is not None else None,
            "packageLengthIn":      float(self.package_length_in) if self.package_length_in is not None else None,
            "packageWidthIn":       float(self.package_width_in) if self.package_width_in is not None else None,
            "packageHeightIn":      float(self.package_height_in) if self.package_height_in is not None else None,
            "paidViaShippo":        bool(self.paid_via_shippo),

            # Email tracking
            "lastCustomerEmailAt":  self.last_customer_email_at.isoformat() if self.last_customer_email_at else None,
            "customerEmailsSent":   self.customer_emails_sent or 0,

            # NOTE: `isV2` (the "NEW store" indicator) is NOT computed here —
            # it depends on a file-backed v2-store list (data/checkout_v2_stores.txt)
            # and is injected by the /admin/orders route after the rows are
            # fetched, to keep this model free of file/IO concerns.
        }


class OrderItem(Base):
    __tablename__ = "order_items"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    order_id       = Column(String(20), ForeignKey("orders.id"), nullable=False)
    product_id     = Column(String(255), nullable=True)
    title          = Column(String(255), nullable=False)
    variant        = Column(String(255), nullable=True)
    qty            = Column(Integer, default=1)
    price          = Column(Numeric(10, 2), nullable=False)   # post-promo unit price
    original_price = Column(Numeric(10, 2), nullable=True)    # pre-discount unit price
    total          = Column(Numeric(10, 2), nullable=False)
    # Cart-item image URL (from Shopify CDN). Optional — older orders may
    # not have it. Used to render the actual product image in the v2
    # confirmation pages' order summary card.
    image_url      = Column(String(500), nullable=True)

    order = relationship("Order", back_populates="items")


class InteracPayment(Base):
    __tablename__ = "interac_payments"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    order_id        = Column(String(20), ForeignKey("orders.id"), unique=True)
    expected_amount = Column(Numeric(10, 2), nullable=False)
    received_amount = Column(Numeric(10, 2), nullable=True)   # actual amount received
    sender_name     = Column(String(255), nullable=True)
    sender_email    = Column(String(255), nullable=True)
    matched_at      = Column(DateTime, nullable=True)
    raw_email_id    = Column(String(255), nullable=True, unique=True)   # Gmail message ID
    status          = Column(
        Enum("waiting", "matched", "unmatched", "manual", "underpaid"),
        default="waiting"
    )
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime, server_default=func.now())

    order = relationship("Order", back_populates="interac_payment")


class CryptoInvoice(Base):
    __tablename__ = "crypto_invoices"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    order_id           = Column(String(20), ForeignKey("orders.id"), unique=True)
    btcpay_invoice_id  = Column(String(255), unique=True, nullable=False)
    btcpay_invoice_url = Column(Text, nullable=True)
    coin               = Column(String(20), nullable=True)    # filled after customer selects
    amount_crypto      = Column(Numeric(20, 8), nullable=True)
    amount_fiat        = Column(Numeric(10, 2), nullable=False)
    received_fiat      = Column(Numeric(10, 2), nullable=True)
    status             = Column(String(50), default="New")
    expires_at         = Column(DateTime, nullable=True)
    settled_at         = Column(DateTime, nullable=True)
    created_at         = Column(DateTime, server_default=func.now())

    order = relationship("Order", back_populates="crypto_invoice")


class ZellePayment(Base):
    """US equivalent of InteracPayment. Customer sends Zelle to a US account."""
    __tablename__ = "zelle_payments"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    order_id        = Column(String(20), ForeignKey("orders.id"), unique=True)
    expected_amount = Column(Numeric(10, 2), nullable=False)
    received_amount = Column(Numeric(10, 2), nullable=True)   # actual amount received
    sender_name     = Column(String(255), nullable=True)
    sender_email    = Column(String(255), nullable=True)
    matched_at      = Column(DateTime, nullable=True)
    raw_email_id    = Column(String(255), nullable=True, unique=True)
    status          = Column(
        Enum("waiting", "matched", "unmatched", "manual", "underpaid"),
        default="waiting"
    )
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime, server_default=func.now())

    order = relationship("Order", back_populates="zelle_payment")


class CustomerEmailLog(Base):
    """Audit log of every customer-facing email — both admin-sent (reminder,
    underpaid) and automatic (confirmation, sent by services/order_finalize.py
    on every successful payment, sent_by="system").
    """
    __tablename__ = "customer_email_log"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    order_id   = Column(String(20), nullable=False, index=True)
    email_type = Column(Enum("reminder", "underpaid", "confirmation"), nullable=False, index=True)
    sent_to    = Column(String(255), nullable=False)
    subject    = Column(String(255), nullable=False)
    body_text  = Column(Text, nullable=True)
    body_html  = Column(Text, nullable=True)
    sent_by    = Column(String(100), default="admin")
    success    = Column(Integer, default=1)
    sent_at    = Column(DateTime, server_default=func.now(), index=True)
    
class NowPaymentsInvoice(Base):
    __tablename__ = "nowpayments_invoices"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    order_id      = Column(String(20), ForeignKey("orders.id"), unique=True)
    np_invoice_id = Column(String(255), unique=True, nullable=False)
    np_payment_id = Column(String(255), nullable=True)
    invoice_url   = Column(Text, nullable=True)
    coin          = Column(String(50), nullable=True)
    amount_fiat   = Column(Numeric(10, 2), nullable=False)
    received_fiat = Column(Numeric(10, 2), nullable=True)
    status        = Column(String(50), default="waiting")
    settled_at    = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, server_default=func.now())

    order = relationship("Order", back_populates="nowpayments_invoice")