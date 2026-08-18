"""
Admin authentication routes - server-side session based
"""
import logging
import os
import secrets
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME") or ""
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be set in .env")

# View-only login — same dashboard, but every mutating admin endpoint 403s
# (see require_write_access below). Optional: blank credentials just means
# this login never matches anything, so it's a no-op if unset.
VIEWER_USERNAME = os.getenv("VIEWER_USERNAME") or ""
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD") or ""

REDIS_URL       = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
SESSION_TTL_SEC = 2 * 3600
SESSION_COOKIE  = "admin_session"
SESSION_PREFIX  = "admin_sess:"

_redis: Optional[aioredis.Redis] = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis

async def create_session(role: str = "admin") -> str:
    token = secrets.token_hex(32)
    r = await get_redis()
    # Value used to be the literal string "1" (a boolean flag with no role
    # info). Now stores the role ("admin" or "viewer") — validate_session
    # still treats any non-empty value as "logged in", so old sessions from
    # before this change would be misread as garbage roles; that's fine,
    # they're all short-lived (SESSION_TTL_SEC) and just get treated as
    # non-admin until they expire naturally.
    await r.set(SESSION_PREFIX + token, role, ex=SESSION_TTL_SEC)
    return token

async def get_session_role(token: Optional[str]) -> Optional[str]:
    """Returns the session's role ("admin"/"viewer") or None if not logged in."""
    if not token:
        return None
    r = await get_redis()
    return await r.get(SESSION_PREFIX + token)

async def validate_session(token: Optional[str]) -> bool:
    return await get_session_role(token) is not None

async def delete_session(token: str) -> None:
    r = await get_redis()
    await r.delete(SESSION_PREFIX + token)


# ── Login rate limiting ─────────────────────────────────────────────────────
# The password check itself is timing-safe (secrets.compare_digest), but
# nothing previously stopped unlimited guesses — same IP-based sliding-window
# approach used for checkout submissions (routes/checkout.py).
LOGIN_RATE_LIMIT_MAX    = 8     # attempts...
LOGIN_RATE_LIMIT_WINDOW = 300   # ...per this many seconds, per IP

def _client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_login_rate_limit(request: Request) -> None:
    try:
        r = await get_redis()
        key = f"admin_login_ratelimit:{_client_ip(request)}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, LOGIN_RATE_LIMIT_WINDOW)
        if count > LOGIN_RATE_LIMIT_MAX:
            raise HTTPException(429, "Too many login attempts — please wait a few minutes and try again.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[admin-login rate-limit] check failed, allowing request: {e}")


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/peps-admin-2026/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if await validate_session(token):
        return RedirectResponse("/peps-admin-2026/dashboard")
    return FileResponse("static/admin-login.html")


@router.get("/peps-admin-2026/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await validate_session(token):
        return RedirectResponse("/peps-admin-2026/login")
    return FileResponse("static/admin-dashboard.html")


@router.get("/peps-admin-2026", response_class=HTMLResponse)
async def admin_redirect(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await validate_session(token):
        return RedirectResponse("/peps-admin-2026/login")
    return FileResponse("static/admin-dashboard.html")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/peps-admin-2026/auth/login", dependencies=[Depends(_check_login_rate_limit)])
async def do_login(body: LoginRequest, response: Response):
    admin_ok = (
        secrets.compare_digest(body.username.encode(), ADMIN_USERNAME.encode())
        and secrets.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    )
    # Only check viewer creds if they're actually configured — compare_digest
    # needs equal-length-safe non-empty strings, and an empty VIEWER_USERNAME
    # would otherwise make an empty submitted username "match".
    viewer_ok = bool(VIEWER_USERNAME and VIEWER_PASSWORD) and (
        secrets.compare_digest(body.username.encode(), VIEWER_USERNAME.encode())
        and secrets.compare_digest(body.password.encode(), VIEWER_PASSWORD.encode())
    )
    if not (admin_ok or viewer_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = await create_session(role="admin" if admin_ok else "viewer")
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        max_age=SESSION_TTL_SEC,
        secure=True,
    )
    return {"success": True, "role": "admin" if admin_ok else "viewer"}


@router.post("/peps-admin-2026/auth/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"success": True}


@router.get("/peps-admin-2026/auth/check")
async def auth_check(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    role = await get_session_role(token)
    if role is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"authenticated": True, "role": role}


async def require_admin(request: Request) -> None:
    """Gates every /admin route — just "is there a valid session", regardless
    of role. Both admin and viewer logins pass this."""
    token = request.cookies.get(SESSION_COOKIE)
    if not await validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")


async def require_write_access(request: Request) -> None:
    """Extra gate for mutating admin routes (mark paid, cancel, recover,
    match payments, edit brands, etc.) — on top of require_admin, which
    already ran at the router level. A viewer session is authenticated but
    not authorized to write, so this 403s rather than 401s."""
    token = request.cookies.get(SESSION_COOKIE)
    role = await get_session_role(token)
    if role != "admin":
        raise HTTPException(status_code=403, detail="View-only account — this action is disabled")