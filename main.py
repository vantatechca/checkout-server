"""
Checkout Server — FastAPI entry point.

Brand middleware:
  Every request reads the Host header, looks up the matching Brand in DB,
  and attaches it to request.state.brand. All downstream routes use this
  to serve the correct store name, logo, colors, discounts, and API keys.

Static file serving:
  GET /              → serves checkout.html (brand-injected)
  GET /order/{id}/confirmation → order confirmation page
  GET /order/success → Stripe embedded checkout thank-you page
  GET /config        → returns brand config JSON for frontend bootstrapping
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, text

from database import engine, AsyncSessionLocal
from models.brand import Brand
from models import Order  # triggers all model registrations
from models.order import NowPaymentsInvoice
import models  # noqa — ensure all models are registered with Base
from routes.checkout import router as checkout_router
from routes.webhooks import router as webhooks_router
from routes.admin    import router as admin_router
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Jinja2 template engine ───────────────────────────────────────────────────
jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"]),
)


def _get_card_enabled_stores() -> list[str]:
    """
    Parse CARD_ENABLED_STORES env var into a clean lowercase list
    (no protocol, no trailing slash).
    """
    raw = getattr(settings, "CARD_ENABLED_STORES", "") or ""
    if not raw:
        return []
    return [
        s.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
        for s in raw.split(",")
        if s.strip()
    ]


def _is_card_enabled_for_source(source_domain: str) -> bool:
    """
    Returns True if credit card payment should be enabled for this source store.

    Rules:
      - CARD_ENABLED_STORES empty or "*" → enabled for ALL source stores
      - Otherwise → source_domain must match one of the entries (substring)
    """
    raw = (getattr(settings, "CARD_ENABLED_STORES", "") or "").strip()

    # Empty or "*" → enabled for all
    if not raw or raw == "*":
        return True

    if not source_domain:
        return True  # No source given but cards are globally enabled

    src = source_domain.lower().replace("https://", "").replace("http://", "").rstrip("/")
    enabled_stores = _get_card_enabled_stores()
    if not enabled_stores:
        return True

    for store in enabled_stores:
        if store and store in src:
            return True
    return False


# ─── Bridge-7 availability cache ──────────────────────────────────────────────
# Avoid hammering bridge-7's /router/status on every page load.
# Result is cached for BRIDGE_CHECK_CACHE_TTL seconds.
BRIDGE_CHECK_CACHE_TTL = 30  # seconds
_bridge_cache: dict = {"ts": 0.0, "available": True}


async def _is_bridge_card_available() -> bool:
    """
    Returns True if bridge-7 has at least one available checkout store
    (Stripe or Shopify) under its daily limit. False if all are exhausted.

    Caches the result for BRIDGE_CHECK_CACHE_TTL seconds. On any error
    (network, timeout, bad response) returns True (fail-open) so the
    checkout flow isn't blocked by transient infrastructure issues.
    """
    now = time.time()
    if (now - _bridge_cache["ts"]) < BRIDGE_CHECK_CACHE_TTL:
        return _bridge_cache["available"]

    # Derive status URL from BRIDGE_URL (replace /s2s with /router/status)
    bridge_url = getattr(settings, "BRIDGE_URL", "") or ""
    if not bridge_url:
        return True
    status_url = bridge_url.replace("/s2s", "/router/status")

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(status_url)
        if resp.status_code != 200:
            logger.warning(f"Bridge status returned {resp.status_code} — assuming cards available")
            _bridge_cache.update(ts=now, available=True)
            return True

        data = resp.json()
        available_count = int(data.get("available", 0))
        is_available = available_count > 0

        _bridge_cache.update(ts=now, available=is_available)
        if not is_available:
            logger.warning(f"🚫 Bridge reports ALL stores exhausted — disabling card option")
        return is_available

    except Exception as e:
        logger.warning(f"Bridge availability check failed ({e}) — assuming cards available")
        _bridge_cache.update(ts=now, available=True)
        return True


# ─── App lifespan (startup / shutdown) ───────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify DB connection on startup
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        logger.info("✅ Database connection OK")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")

    # Auto-create missing tables from models (dev convenience)
    try:
        from database import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Tables verified/created")
    except Exception as e:
        logger.error(f"❌ Auto-create tables failed: {e}")

    # Order expiry task DISABLED — orders stay pending until admin acts on them.
    # Customers may complete Interac/Zelle hours or days after placing the order;
    # we don't want a timer silently flipping those to expired.

    # Log card-enabled stores for visibility
    raw_enabled = (getattr(settings, "CARD_ENABLED_STORES", "") or "").strip()
    if not raw_enabled or raw_enabled == "*":
        logger.info("💳 Card payment enabled for ALL source stores")
    else:
        enabled_stores = _get_card_enabled_stores()
        logger.info(f"💳 Card payment enabled for: {enabled_stores}")

    # Log Stripe publishable key status
    if settings.STRIPE_PUBLISHABLE_KEY:
        logger.info(f"💳 Stripe publishable key configured (starts with: {settings.STRIPE_PUBLISHABLE_KEY[:10]}...)")
    else:
        logger.warning("⚠️  STRIPE_PUBLISHABLE_KEY not set — embedded Stripe checkout will fail")

    yield

    # Cleanup
    await engine.dispose()
    logger.info("Database engine disposed.")


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Checkout Server",
    version="1.0.0",
    docs_url="/api/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# CORS — only needed if checkout page is served from a different origin
# (not needed if Nginx serves everything from same domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pepscheckoutportal.com",
        "https://www.pepscheckoutportal.com",
        "https://eaststpaulpeptides.ca",
        "https://swiftremit.ca",
        "https://www.swiftremit.ca",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ─── Security headers ─────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"]    = "nosniff"
        # response.headers["X-Frame-Options"]           = "DENY"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    return response


# ─── Brand middleware ─────────────────────────────────────────────────────────
@app.middleware("http")
async def brand_middleware(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Brand).where(Brand.domain == host, Brand.active == True)
        )
        brand = result.scalar_one_or_none()

    request.state.brand = brand

    if brand is None and settings.ENVIRONMENT == "production":
        logger.warning(f"Unknown domain: {host}")
        # Still serve the page — will use defaults

    response = await call_next(request)
    return response


# ─── Routers ─────────────────────────────────────────────────────────────────
app.include_router(checkout_router)
app.include_router(webhooks_router)
app.include_router(admin_router)
from routes.auth_routes import router as auth_router
app.include_router(auth_router)
from routes.revenue import router as revenue_router
app.include_router(revenue_router)

# Static files (CSS, JS, images if any)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Brand config endpoint ────────────────────────────────────────────────────
@app.get("/config")
async def brand_config(request: Request):
    """
    Returns brand configuration as JSON.
    Called by the checkout page JS on load to dynamically apply branding.
    """
    brand = getattr(request.state, "brand", None)

    if brand:
        config = brand.to_public_dict()
    else:
        # Fallback defaults when domain isn't registered
        config = {
            "storeName":       "Checkout",
            "logoUrl":         None,
            "headerBgUrl":     None,
            "accentColor":     "#dd1d1d",
            "accentHover":     "#b01515",
            "interacEmail":    settings.INTERAC_DEFAULT_EMAIL,
            "interacDiscount": 10.0,
            "cryptoDiscount":  10.0,
        }

    return JSONResponse(config)

# ─── Whop availability helper ─────────────────────────────────────────────────
async def _is_whop_available_today() -> bool:
    """
    Returns True if Whop is configured AND today's CAD volume routed through
    Whop is still under WHOP_DAILY_LIMIT. False otherwise.

    Used by /api/checkout/whop-embed (hard enforcement) and by the checkout
    page renderer (hide the Card (WHOP) option entirely when capacity is
    reached, so customers don't see a button that just errors).

    Counts orders where payment_method=card AND payment_ref starts with "ch_"
    (Whop session ID prefix) created since UTC midnight today.
    """
    # Master kill-switch — hides Whop without touching keys or limits.
    # Set WHOP_ENABLED=false in .env to disable, then restart.
    if not bool(getattr(settings, "WHOP_ENABLED", True)):
        return False

    # Whop not configured at all → option hidden
    if bool(getattr(settings, "WHOP_SANDBOX", False)):
        configured = bool(getattr(settings, "WHOP_SANDBOX_API_KEY", ""))
    else:
        configured = bool(getattr(settings, "WHOP_API_KEY", ""))
    if not configured:
        return False

    daily_limit = float(getattr(settings, "WHOP_DAILY_LIMIT", 0) or 0)
    if daily_limit <= 0:
        # 0 means disabled — but configured. We treat as "always available".
        # If you want 0 to mean "always hidden", flip this to `return False`.
        return True

    try:
        from datetime import datetime, timezone
        from sqlalchemy import select, func
        from models.order import Order, PaymentMethod
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.coalesce(func.sum(Order.total), 0))
                .where(Order.created_at >= today_start)
                .where(Order.payment_method == PaymentMethod.card)
                .where(Order.payment_ref.like("ch_%"))
            )
            today_total = float(result.scalar() or 0)
        return today_total < daily_limit
    except Exception as e:
        # On any DB error, default to AVAILABLE (fail-open). Better to show
        # the option and have a transaction fail than to silently hide it
        # because of a transient DB blip.
        logger.warning(f"[Whop availability] check failed ({e}) — defaulting to available")
        return True


# ─── Checkout page ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def checkout_page(request: Request):
    """
    Serve the checkout HTML with brand vars injected server-side.
    This avoids a visible flash/reflow from client-side brand loading.
    """
    brand = getattr(request.state, "brand", None)

    # Country from theme's query param (?country=US or CA). Defaults to CA.
    country = request.query_params.get("country", "CA").upper()
    currency = "USD" if country == "US" else "CAD"

    # Source store determines if credit card is enabled (and default)
    source_domain = request.query_params.get("source", "")

    source_allowed = _is_card_enabled_for_source(source_domain)
    card_enabled = source_allowed   # per-store whitelist (pymtz)

    # Whop availability — checked on page load so the option only renders
    # when there's still daily capacity. Otherwise customers see a button
    # that just errors out, which is bad UX.
    whop_enabled = card_enabled and await _is_whop_available_today()

    ctx = {
        "store_name": (
            request.query_params.get("storename") + " Checkout"
            if request.query_params.get("storename")
            else (brand.store_name if brand else "Checkout")
        ),
        "logo_url":         brand.logo_url          if brand else "",
        "header_bg_url":    brand.header_bg_url     if brand else "",
        "accent_color":     brand.accent_color      if brand else "#dd1d1d",
        "accent_hover":     brand.accent_hover      if brand else "#b01515",
        "interac_email":    brand.interac_email     if brand else settings.INTERAC_DEFAULT_EMAIL,
        "zelle_email":      settings.ZELLE_DEFAULT_EMAIL,
        "interac_discount": float(brand.interac_discount if brand else 10),
        "crypto_discount":  float(brand.crypto_discount  if brand else 10),
        "store_country":    country,
        "store_currency":   currency,
        "base_url":         settings.BASE_URL,
        "source_domain":    source_domain,
        "card_enabled":     card_enabled,
        "whop_enabled":     whop_enabled,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY or "",
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
    }

    template = jinja_env.get_template("checkout.html")
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Stripe embedded checkout — branded thank-you page ────────────────────────
@app.get("/order/success", response_class=HTMLResponse)
async def order_success_page(request: Request):
    """
    Branded thank-you page for Stripe embedded checkout success.
    Stripe redirects here with ?session_id=cs_test_xxx after payment.
    The page then fetches order details from the Stripe worker and displays
    a summary using the customer's form data (not Stripe's invoice data).
    """
    brand = getattr(request.state, "brand", None)
    session_id = request.query_params.get("session_id", "")

    ctx = {
        "store_name":        brand.store_name   if brand else "Checkout",
        "logo_url":          brand.logo_url     if brand else "",
        "accent_color":      brand.accent_color if brand else "#dc2626",
        "accent_hover":      brand.accent_hover if brand else "#b91c1c",
        "session_id":        session_id,
        "stripe_worker_url": settings.STRIPE_WORKER_URL,
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
    }

    template = jinja_env.get_template("order-success.html")
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Order confirmation page ──────────────────────────────────────────────────
@app.get("/order/{order_id}/confirmation", response_class=HTMLResponse)
async def confirmation_page(order_id: str, request: Request):
    brand = getattr(request.state, "brand", None)

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        result = await db.execute(
            select(Order).where(Order.id == order_id)
            .options(selectinload(Order.items))
            .options(selectinload(Order.nowpayments_invoice))
        )
        order = result.scalar_one_or_none()

    if not order:
        return HTMLResponse("<h1>Order not found.</h1>", status_code=404)

    # Normalize enum values to plain strings for the template
    pm_value = order.payment_method.value if hasattr(order.payment_method, 'value') else str(order.payment_method)
    ps_value = order.payment_status.value if hasattr(order.payment_status, 'value') else str(order.payment_status)

    # pymtz card orders: payment_method=card, payment_ref starts with "pay_"
    # Need separate confirmation flow since pymtz has no webhooks — we verify
    # status on the return-url page via /api/checkout/pymtz-verify/{order_id}.
    is_pymtz = (
        pm_value == "card"
        and bool(order.payment_ref)
        and str(order.payment_ref).startswith("pay_")
    )

    ctx = {
        "store_name":    brand.store_name if brand else order.store_name,
        "logo_url":      brand.logo_url   if brand else "",
        "accent_color":  brand.accent_color if brand else "#dd1d1d",
        "order":         order,
        "order_id":      order_id,
        "payment_method": pm_value,
        "payment_status": ps_value,
        "subtotal":       float(order.subtotal),
        "discount_pct":            float(order.discount_pct or 0),
        "discount_amount":         float(order.discount_amount or 0),
        "total":                   float(order.total),
        "original_subtotal":       float(order.original_subtotal or order.subtotal),
        "discount_code":           order.discount_code or "",
        "voucher_discount":        float(order.promo_discount_amount or 0),
        "voucher_discount_pct":    float(order.promo_discount_pct or 0),
        "payment_method_discount": float(order.discount_amount or 0),
        "interac_discount_pct":    float(brand.interac_discount if brand else 10),
        "zelle_discount_pct":      float(brand.interac_discount if brand else 10),
        "currency":       order.currency,
        "interac_email": (
            brand.interac_email if brand and brand.interac_email
            else settings.INTERAC_DEFAULT_EMAIL
        ),
        "zelle_email":   settings.ZELLE_DEFAULT_EMAIL,
        "btcpay_url": order.payment_ref and f"{settings.BTCPAY_URL}/i/{order.payment_ref}" if pm_value == "crypto" else "",
        "items": order.items if order.items else [],
        "np_invoice_id": order.nowpayments_invoice.np_invoice_id if order.nowpayments_invoice else "",
        "is_pymtz":      is_pymtz,
    }

    if pm_value == "crypto":
        template = jinja_env.get_template("confirmation_crypto.html")
    elif pm_value == "altcoin":
        template = jinja_env.get_template("confirmation_altcoin.html")
    else:
        template = jinja_env.get_template("confirmation.html")
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.ENVIRONMENT}


# ─── Lasso checkout proxy ─────────────────────────────────────────────────────
# Routes /pay?sid=XXX through our server instead of sending the customer
# directly to Lasso's checkout URL. Injects CSS to hide the order summary
# so the customer never sees the decoy product name.
#
# Usage: set LASSO_CHECKOUT_URL=https://pepscheckoutportal.com in .env
# so LassoClient.build_redirect_url() returns /pay?sid=... pointing here.
# This endpoint then fetches the real Lasso page and serves it sanitised.

_LASSO_SUPPRESS_CSS = """
<style id="lasso-portal-overrides">
  /*
   * Hides the order summary / cart review panel on Lasso's checkout page.
   * Selectors are broad to survive Lasso's CSS-module class hashing.
   * Inspect checkout DOM and add specific selectors below if needed.
   */

  /* Common order summary containers */
  [class*="order-summary"],
  [class*="OrderSummary"],
  [class*="order_summary"],
  [class*="cart-summary"],
  [class*="CartSummary"],
  [class*="cart_summary"],
  [class*="product-list"],
  [class*="ProductList"],
  [class*="line-items"],
  [class*="LineItems"],
  [class*="line_items"],
  [class*="cart-items"],
  [class*="CartItems"],
  [id*="order-summary"],
  [id*="cart-summary"],
  [id*="line-items"] {
    display: none !important;
    visibility: hidden !important;
  }

  /* Hide any expandable order toggle / accordion */
  [class*="order-toggle"],
  [class*="summary-toggle"],
  [class*="collapse-summary"],
  [aria-label*="Order summary"],
  [aria-label*="order summary"] {
    display: none !important;
  }
</style>
"""

@app.get("/pay", response_class=HTMLResponse)
async def lasso_proxy(request: Request, sid: str = ""):
    """
    Fetches Lasso's checkout page for the given session, injects CSS to
    suppress the order summary, and serves the result to the customer.
    The customer's browser still runs all of Lasso's JS normally — only
    the visual order summary panel is hidden.
    """
    if not sid:
        return HTMLResponse("<h1>Invalid checkout session.</h1>", status_code=400)

    lasso_checkout_base = getattr(settings, "LASSO_CHECKOUT_URL", "").rstrip("/")
    if not lasso_checkout_base:
        return HTMLResponse("<h1>Checkout unavailable.</h1>", status_code=503)

    # If LASSO_CHECKOUT_URL points back to us (proxy mode), we need the REAL
    # Lasso URL stored separately as LASSO_REAL_CHECKOUT_URL.
    real_checkout_url = (
        getattr(settings, "LASSO_REAL_CHECKOUT_URL", "") or lasso_checkout_base
    )
    target_url = f"{real_checkout_url}?sid={sid}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
        ) as client:
            resp = await client.get(
                target_url,
                headers={
                    "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
    except httpx.RequestError as e:
        logger.error(f"[LassoProxy] Failed to fetch {target_url}: {e}")
        return HTMLResponse("<h1>Checkout temporarily unavailable. Please try again.</h1>", status_code=502)

    html = resp.text

    # Rewrite relative asset URLs → absolute so they still resolve from our domain
    lasso_origin = real_checkout_url.split("/checkout")[0]
    html = html.replace('src="/', f'src="{lasso_origin}/')
    html = html.replace("src='/", f"src='{lasso_origin}/")
    html = html.replace('href="/', f'href="{lasso_origin}/')
    html = html.replace("href='/", f"href='{lasso_origin}/")

    # Inject suppression CSS before </head>
    if "</head>" in html:
        html = html.replace("</head>", f"{_LASSO_SUPPRESS_CSS}</head>", 1)
    else:
        # Fallback: prepend at top
        html = _LASSO_SUPPRESS_CSS + html

    return HTMLResponse(content=html, status_code=200)