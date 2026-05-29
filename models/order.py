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
    Enum, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class PaymentMethod(str, enum.Enum):
    card    = "card"
    interac = "interac"
    crypto  = "crypto"
    zelle   = "zelle"
    altcoin = "altcoin"


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

    # --- Payment ---
    payment_method  = Column(Enum(PaymentMethod), nullable=False)
    payment_status  = Column(Enum(PaymentStatus), default=PaymentStatus.pending, index=True)
    payment_ref     = Column(String(255), nullable=True)  # Helcim/Stripe txn ID, BTCPay invoice ID, etc.
    payment_notes   = Column(Text, nullable=True)
    paid_at         = Column(DateTime, nullable=True)

    # --- Customer email tracking ---
    last_customer_email_at = Column(DateTime, nullable=True)
    customer_emails_sent   = Column(Integer, default=0)

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
    )

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "storeName":     self.store_name,
            "sourceDomain":  self.source_domain,
            "email":         self.email,
            "firstName":     self.first_name,
            "lastName":      self.last_name,
            "phone":         self.phone,
            "address1":      self.address1,
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

            # Payment
            "paymentMethod":        self.payment_method,
            "paymentStatus":        self.payment_status,
            "paidAt":               self.paid_at.isoformat() if self.paid_at else None,
            "createdAt":            self.created_at.isoformat() if self.created_at else None,

            # Email tracking
            "lastCustomerEmailAt":  self.last_customer_email_at.isoformat() if self.last_customer_email_at else None,
            "customerEmailsSent":   self.customer_emails_sent or 0,
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
    """Audit log of customer-facing emails sent by admins.

    Examples: "still waiting for your Interac" reminder, "you paid less than expected" underpaid notice.
    """
    __tablename__ = "customer_email_log"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    order_id   = Column(String(20), nullable=False, index=True)
    email_type = Column(Enum("reminder", "underpaid"), nullable=False)
    sent_to    = Column(String(255), nullable=False)
    subject    = Column(String(255), nullable=False)
    body_text  = Column(Text, nullable=True)
    body_html  = Column(Text, nullable=True)
    sent_by    = Column(String(100), default="admin")
    success    = Column(Integer, default=1)
    sent_at    = Column(DateTime, server_default=func.now())
    
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