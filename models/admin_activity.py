"""
AdminActivity — audit log of admin actions.

Every security-sensitive admin action (mark-paid, unmark-paid, cancel,
recover, CSV export, etc.) writes a row here so there's a trail of who did
what and when. Surfaced in the dashboard's "Admin audit log" feed.

Auto-created on startup via Base.metadata.create_all (see main.py lifespan).
"""
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from database import Base


class AdminActivity(Base):
    __tablename__ = "admin_activity"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    admin_user  = Column(String(120), nullable=False, default="admin")
    action      = Column(String(60),  nullable=False, index=True)   # mark_paid, cancel, recover, unmark_paid, export_csv, ...
    target_type = Column(String(40),  nullable=True)                # "order", "orders", ...
    target_id   = Column(String(60),  nullable=True)                # order id, etc.
    details     = Column(Text,        nullable=True)                # free-text context (capped by caller)
    ip_address  = Column(String(64),  nullable=True)
