from sqlalchemy import Column, Integer, String, Text, Boolean, Numeric, DateTime
from sqlalchemy.sql import func
from database import Base


class Brand(Base):
    __tablename__ = "brands"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    domain          = Column(String(255), unique=True, nullable=False, index=True)
    store_name      = Column(String(255), nullable=False)
    logo_url        = Column(Text, nullable=True)
    header_bg_url   = Column(Text, nullable=True)
    accent_color    = Column(String(20), default="#dd1d1d")
    accent_hover    = Column(String(20), default="#b01515")

    # Interac
    interac_email   = Column(String(255), nullable=True)
    interac_discount = Column(Numeric(5, 2), default=5.00)

    # Crypto
    crypto_discount  = Column(Numeric(5, 2), default=10.00)

    # Card processor (Helcim) — per-brand override
    helcim_api_key  = Column(Text, nullable=True)

    # BTCPay — per-brand store override (optional, defaults to global)
    btcpay_store_id = Column(String(255), nullable=True)

    # Source origin for CORS / iframe embed validation
    allowed_origins = Column(Text, nullable=True)   # comma-separated

    active          = Column(Boolean, default=True)
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def to_public_dict(self) -> dict:
        """Safe config returned to frontend — NO secret keys."""
        return {
            "storeName":       self.store_name,
            "logoUrl":         self.logo_url,
            "headerBgUrl":     self.header_bg_url,
            "accentColor":     self.accent_color,
            "accentHover":     self.accent_hover,
            "interacEmail":    self.interac_email,
            "interacDiscount": float(self.interac_discount or 5),
            "cryptoDiscount":  float(self.crypto_discount or 10),
        }
