"""
Customer account service — upsert + verify for the "soft account" feature.

Uses Python stdlib hashlib.pbkdf2_hmac for password hashing. No new dependency.
Hash format (stored as the password_hash column):

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

Iterations: 200_000 (reasonable for 2024–2026 hardware). Salt: 16 random bytes.

This service is intentionally narrow:
  • upsert_account(email, password, profile) — called after a paid order if
    the customer typed a password during checkout. Inserts or overwrites the
    row. Latest profile wins on update.
  • verify_and_fetch(email, password) — called by /api/customer/lookup. Returns
    the saved profile dict on success, None on bad email OR bad password.
    Same return value for both failure modes so callers don't leak which one.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.customer_account import CustomerAccount

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGO       = "sha256"
PBKDF2_SALT_BYTES = 16


def _hash_password(password: str) -> str:
    """Return a PBKDF2-SHA256 hash string in the format we store."""
    salt = os.urandom(PBKDF2_SALT_BYTES)
    dk   = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a password against a stored hash string."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt  = bytes.fromhex(salt_hex)
        want  = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    got = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(got, want)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


async def upsert_account(
    db: AsyncSession,
    *,
    email:    str,
    password: str,
    profile:  dict,
) -> Optional[CustomerAccount]:
    """
    Insert or overwrite a customer_accounts row. Latest checkout wins on the
    saved profile fields — that way the prefill always reflects the most
    recent address the customer used.

    Best-effort: any error is logged + swallowed (returns None). Caller must
    NOT depend on this for order recording.
    """
    try:
        email_n = _norm_email(email)
        if not email_n or not password:
            return None

        pw_hash = _hash_password(password)

        result = await db.execute(
            select(CustomerAccount).where(CustomerAccount.email == email_n)
        )
        row = result.scalar_one_or_none()

        if row:
            row.password_hash = pw_hash
            # Update saved profile fields — only overwrite when the new value
            # is non-empty so a partial form doesn't wipe out a complete
            # previously-saved profile.
            for key in ("first_name", "last_name", "phone",
                        "address1", "address2", "city",
                        "province", "postal_code", "country"):
                v = (profile.get(key) or "").strip()
                if v:
                    setattr(row, key, v)
        else:
            row = CustomerAccount(
                email         = email_n,
                password_hash = pw_hash,
                first_name    = (profile.get("first_name") or "").strip() or None,
                last_name     = (profile.get("last_name")  or "").strip() or None,
                phone         = (profile.get("phone")      or "").strip() or None,
                address1      = (profile.get("address1")   or "").strip() or None,
                address2      = (profile.get("address2")   or "").strip() or None,
                city          = (profile.get("city")       or "").strip() or None,
                province      = (profile.get("province")   or "").strip() or None,
                postal_code   = (profile.get("postal_code")or "").strip() or None,
                country       = (profile.get("country")    or "").strip() or None,
            )
            db.add(row)

        await db.flush()
        logger.info(f"[customer_accounts] upserted account for {email_n}")
        return row

    except Exception as e:
        logger.warning(f"[customer_accounts] upsert failed for {email!r}: {e}")
        return None


async def verify_and_fetch(
    db: AsyncSession,
    *,
    email:    str,
    password: str,
) -> Optional[dict]:
    """
    Verify the (email, password) pair and return a dict of saved profile
    fields suitable for prefilling the checkout form. Returns None on any
    failure (bad email, bad password, DB error) — caller surfaces a generic
    "invalid email or password" message.
    """
    try:
        email_n = _norm_email(email)
        if not email_n or not password:
            return None

        result = await db.execute(
            select(CustomerAccount).where(CustomerAccount.email == email_n)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None

        if not _verify_password(password, row.password_hash or ""):
            return None

        return {
            "email":       row.email,
            "first_name":  row.first_name  or "",
            "last_name":   row.last_name   or "",
            "phone":       row.phone       or "",
            "address1":    row.address1    or "",
            "address2":    row.address2    or "",
            "city":        row.city        or "",
            "province":    row.province    or "",
            "postal_code": row.postal_code or "",
            "country":     row.country     or "",
        }

    except Exception as e:
        logger.warning(f"[customer_accounts] verify failed for {email!r}: {e}")
        return None
