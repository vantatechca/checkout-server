"""
Admin authentication routes - server-side session based
"""
import os
import secrets
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from pydantic import BaseModel

router = APIRouter()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME") or ""
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be set in .env")

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

async def create_session() -> str:
    token = secrets.token_hex(32)
    r = await get_redis()
    await r.set(SESSION_PREFIX + token, "1", ex=SESSION_TTL_SEC)
    return token

async def validate_session(token: Optional[str]) -> bool:
    if not token:
        return False
    r = await get_redis()
    return await r.get(SESSION_PREFIX + token) is not None

async def delete_session(token: str) -> None:
    r = await get_redis()
    await r.delete(SESSION_PREFIX + token)


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


@router.post("/peps-admin-2026/auth/login")
async def do_login(body: LoginRequest, response: Response):
    user_ok = secrets.compare_digest(body.username.encode(), ADMIN_USERNAME.encode())
    pass_ok = secrets.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = await create_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        max_age=SESSION_TTL_SEC,
        secure=True,
    )
    return {"success": True}


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
    if not await validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"authenticated": True}


async def require_admin(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not await validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")