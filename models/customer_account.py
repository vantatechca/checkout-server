"""
CustomerAccount — lightweight "soft account" used purely to prefill the
checkout form on a return visit. NOT linked to Shopify, NOT used for order
history, NOT used for login to anything else. The customer types a password
on their first checkout and the next time they come back they can click
"Returning customer? Sign in" to autofill name/phone/address.

Password is stored as a bcrypt hash. No plaintext is ever persisted.
"""
from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.sql import func
from database import Base


class CustomerAccount(Base):
    __tablename__ = "customer_accounts"

    # Email is the primary key — lowercased + stripped at write time so
    # "Foo@Bar.com" and "foo@bar.com" collapse to the same row.
    email         = Column(String(255), primary_key=True)
    password_hash = Column(String(255), nullable=False)

    # Saved profile — what we prefill on return visit.
    first_name  = Column(String(100), nullable=True)
    last_name   = Column(String(100), nullable=True)
    phone       = Column(String(50),  nullable=True)
    address1    = Column(String(255), nullable=True)
    address2    = Column(String(255), nullable=True)
    city        = Column(String(100), nullable=True)
    province    = Column(String(100), nullable=True)
    postal_code = Column(String(20),  nullable=True)
    country     = Column(String(2),   nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_customer_accounts_updated", "updated_at"),
    )
