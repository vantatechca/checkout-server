"""
Admin audit-log table — every action taken from the admin dashboard is recorded
here so the Dashboard tab can show "who did what when".
"""
from sqlalchemy import Column, BigInteger, String, Text, DateTime
from sqlalchemy.sql import func

from database import Base


class AdminActivity(Base):
    __tablename__ = "admin_activities"

    id          = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)
    admin_user  = Column(String(64), nullable=True, index=True)   # username from auth, "system" for non-user-triggered
    action      = Column(String(64), nullable=False, index=True)  # mark_paid, unmark_paid, cancel, recover, bulk_*, export, login, logout, ...
    target_type = Column(String(32), nullable=True)               # "order", "brand", "system", etc.
    target_id   = Column(String(64), nullable=True, index=True)   # ORD-XXX, brand id, etc.
    details     = Column(Text, nullable=True)                     # free-text — reason, prev status, count, etc.
    ip_address  = Column(String(64), nullable=True)
