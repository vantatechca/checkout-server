"""
Visit — one row per checkout-page load, logged in main.py's checkout_page().

Write-once, immutable log: never updated after insert. A visit's eventual
outcome (no order / pending / paid) is computed at read time by joining
Order.visitor_id == Visit.visitor_id (see GET /admin/visits in
routes/admin.py) rather than storing order_id here — that keeps this
table a simple append-only log and always reflects the order's current
status without a second write to keep in sync.
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base


class Visit(Base):
    __tablename__ = "visits"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    visitor_id    = Column(String(32), nullable=False, index=True)   # cs_vid cookie value
    brand_id      = Column(Integer, ForeignKey("brands.id"), nullable=True)
    store_name    = Column(String(255), nullable=True)   # denormalized, same rationale as Order.store_name
    source_domain = Column(String(255), nullable=True, index=True)
    ip_address    = Column(String(45), nullable=True)
    country       = Column(String(2), nullable=True, index=True)     # from Cloudflare's CF-IPCountry header
    # City/region are NOT stored here — they're resolved lazily, on
    # admin view, from the ip_geo_cache table (models/ip_geo_cache.py),
    # keyed by ip_address rather than per-visit. See services/geoip.py.
    user_agent    = Column(Text, nullable=True)
    referrer      = Column(Text, nullable=True)
    created_at    = Column(DateTime, server_default=func.now(), index=True)
