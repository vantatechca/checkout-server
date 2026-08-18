"""
Customer-account lookup endpoint.

POST /api/customer/lookup
  Body: { "email": "...", "password": "..." }
  Returns:
    200 { "ok": True,  "profile": { first_name, ..., country } } on success
    200 { "ok": False, "error": "invalid_credentials" } on bad creds

We return 200 with ok=False (not 401) on bad credentials so the frontend has
a uniform fetch path — checkout is a customer-facing surface and we don't
want random 4xx responses generating browser console errors.

Anti-abuse:
  • Generic error message on every failure (no "email not found" vs "wrong
    password" disclosure).
  • Short timeout on the bcrypt verify.
  • TODO if abuse: add a per-IP rate limit. Currently not rate-limited —
    we'll add Redis-backed throttling if we see scraping.
"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from services.customer_accounts import verify_and_fetch

router = APIRouter(prefix="/api/customer", tags=["customer"])
logger = logging.getLogger(__name__)


class LookupRequest(BaseModel):
    email:    str = Field(..., max_length=255)
    password: str = Field(..., min_length=1, max_length=64)


@router.post("/lookup")
async def customer_lookup(
    payload: LookupRequest,
    db:      AsyncSession = Depends(get_db),
):
    profile = await verify_and_fetch(
        db,
        email    = payload.email,
        password = payload.password,
    )

    if not profile:
        return {"ok": False, "error": "invalid_credentials"}

    return {"ok": True, "profile": profile}
