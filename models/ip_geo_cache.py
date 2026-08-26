"""
IpGeoCache — one row per distinct IP address ever looked up, keyed by the
IP itself rather than by visit or order. A given IP's location doesn't
change between visits, so caching by IP (not per-Visit) means the same
address is only ever resolved once via the live ip-api.com lookup no
matter how many times it shows up across many Visit/Order rows. See
services/geoip.py for how this is populated and used.
"""
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.sql import func
from database import Base


class IpGeoCache(Base):
    __tablename__ = "ip_geo_cache"

    ip_address   = Column(String(45), primary_key=True)
    city         = Column(String(100), nullable=True)
    region       = Column(String(100), nullable=True)
    country      = Column(String(2), nullable=True)
    # False = looked up but ip-api.com had nothing for this IP (private/
    # reserved range, or the lookup failed) — still cached so we don't
    # keep re-hitting the free tier's rate limit for an IP that will
    # never resolve.
    resolved     = Column(Boolean, nullable=False, default=True)
    looked_up_at = Column(DateTime, server_default=func.now())
