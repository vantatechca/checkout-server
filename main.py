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
import re
import secrets
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
from models.visit import Visit
import models  # noqa — ensure all models are registered with Base
from routes.checkout import router as checkout_router, _client_ip, _looks_like_ip
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


# ─── Brand color overrides via query param ───────────────────────────────────
# A store can pass its own color in the redirect URL — e.g.
#   /?source=victoriapeps.ca&accent=%237c3aed
# This wins over the brand DB row, so a store doesn't need a DB update to
# theme its checkout. Hex codes only (#abc or #aabbcc) — anything else is
# rejected to prevent CSS injection.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _validate_hex_color(raw: str) -> str:
    """Return `raw` if it's a safe hex color, else empty string."""
    if not raw:
        return ""
    raw = raw.strip()
    if _HEX_COLOR_RE.match(raw):
        return raw
    return ""


def _darken_hex(hex_color: str, factor: float = 0.78) -> str:
    """Return `hex_color` darkened by (1-factor). Used to derive a hover shade
    when the store passes a primary color but no explicit hover."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _resolve_accent(request: Request, brand, default_color: str = "",
                    default_hover: str = "") -> tuple[str, str]:
    """Resolve (accent_color, accent_hover) for any page that wants brand colors.

    Priority:
      1. ?accent / ?accent_hover URL params (store-supplied)
      2. brand DB row
      3. Country-based default: red for CA, blue for US (legacy behavior)
      4. Hardcoded red

    Shared by the checkout page, confirmation page, and Stripe success page so
    a customer keeps the same color across the entire flow.
    """
    qp_accent = _validate_hex_color(request.query_params.get("accent", ""))
    qp_hover  = _validate_hex_color(request.query_params.get("accent_hover", ""))
    if qp_accent:
        return (qp_accent, qp_hover or _darken_hex(qp_accent))

    if brand and brand.accent_color:
        return (brand.accent_color, brand.accent_hover or _darken_hex(brand.accent_color))

    # Country-based legacy default. CA → red, US → blue.
    country = (request.query_params.get("country", "CA") or "CA").upper()
    if country == "US":
        return (default_color or "#2563eb", default_hover or "#1d4ed8")
    return (default_color or "#dd1d1d", default_hover or "#b01515")


# ─── V2 checkout routing ─────────────────────────────────────────────────────
# Stores listed in CHECKOUT_V2_STORES_FILE get served the new v2 template
# (templates/checkout-v2.html). File is read once and cached in-process.
#
# Line format:
#     domain.com           ← v2 store, country unspecified (defaults to CA)
#     domain.com:US        ← v2 store, force USD currency
#     domain.com:CA        ← v2 store, force CAD currency
#     # comment
#
# Comparing "is in the file" still works via membership in the dict's keys.
_V2_STORES: dict[str, str | None] | None = None  # {domain: "US" | "CA" | None}
_V2_MTIME: float = 0.0


def _normalize_domain(d: str) -> str:
    d = (d or "").strip().lower().replace("https://", "").replace("http://", "")
    d = d.lstrip("/").rstrip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def _load_v2_stores() -> dict[str, str | None]:
    """Read the v2 stores file. Cached, refreshed on mtime change.

    Returns a {domain: country} dict — `country` is "US", "CA", or None if
    the line had no `:CC` suffix.
    """
    global _V2_STORES, _V2_MTIME
    path = getattr(settings, "CHECKOUT_V2_STORES_FILE", "") or ""
    if not path:
        return {}
    try:
        import os
        mtime = os.path.getmtime(path)
    except OSError:
        _V2_STORES = {}
        return _V2_STORES
    if _V2_STORES is not None and mtime == _V2_MTIME:
        return _V2_STORES
    stores: dict[str, str | None] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Split on `:US` / `:CA` country suffix — but only the LAST
                # colon (so `https://...` doesn't get misparsed as country).
                country: str | None = None
                if ":" in line:
                    head, _, tail = line.rpartition(":")
                    cc = tail.strip().upper()
                    if cc in ("US", "CA"):
                        country = cc
                        line = head
                stores[_normalize_domain(line)] = country
    except OSError:
        pass
    _V2_STORES = stores
    _V2_MTIME = mtime
    return _V2_STORES


def _is_v2_store(source_domain: str) -> bool:
    """True if this source-domain should be served the v2 checkout."""
    if not source_domain:
        return False
    return _normalize_domain(source_domain) in _load_v2_stores()


def _v2_store_country(source_domain: str) -> str | None:
    """Returns "US" or "CA" if the v2 store has a country pinned in the
    file, else None (unknown — caller should fall back to query param)."""
    if not source_domain:
        return None
    return _load_v2_stores().get(_normalize_domain(source_domain))


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


def _stripe_direct_enabled_for(source_domain: str) -> bool:
    """
    Return True if the Stripe direct card option should appear on the
    checkout for the given source store.

    Preconditions that must ALL hold:
      1. STRIPE_DIRECT_ENABLED=true
      2. STRIPE_SECRET_KEY + STRIPE_PUBLISHABLE_KEY are set
      3. source_domain is in STRIPE_DIRECT_STORES allowlist

    Allowlist semantic:
      ""    (empty)  → no stores
      "*"            → all stores
      "a.com,b.com"  → only listed domains
    """
    if not bool(getattr(settings, "STRIPE_DIRECT_ENABLED", False)):
        return False
    if not (getattr(settings, "STRIPE_SECRET_KEY", "") and
            getattr(settings, "STRIPE_PUBLISHABLE_KEY", "")):
        return False

    raw = (getattr(settings, "STRIPE_DIRECT_STORES", "") or "").strip()
    if raw == "*":
        return True
    if raw == "":
        return False
    allowlist = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return (source_domain or "").strip().lower() in allowlist


def _wpay_enabled_for(source_domain: str) -> bool:
    """
    Return True if the WPay hosted-redirect card option should appear on the
    checkout for the given source store.

    Same gating pattern as _stripe_direct_enabled_for():
      1. WPAY_ENABLED=true
      2. WPAY_UID is set — WPAY_USER_TOKEN is NOT required here: the HPP
         payment-submit and status_check requests only ever send `uid`
         (confirmed against WPay's own HPP Postman collection). USER_TOKEN
         is only consulted as an optional extra field on fetch_record's
         fallback call, so its absence shouldn't hide the checkout option.
      3. source_domain is in WPAY_STORES allowlist
    """
    if not bool(getattr(settings, "WPAY_ENABLED", False)):
        return False
    if not getattr(settings, "WPAY_UID", ""):
        return False

    raw = (getattr(settings, "WPAY_STORES", "") or "").strip()
    if raw == "*":
        return True
    if raw == "":
        return False
    allowlist = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return (source_domain or "").strip().lower() in allowlist


def _wpay_2d_enabled_for(source_domain: str, country: str = "") -> bool:
    """
    Return True if "Credit Card (WPay 2D)" — routed through the WordPress
    site's WPay Channels plugin, same as onramp_wp — should appear on the
    checkout for the given source store.

    Separate option from _wpay_enabled_for() (the direct HPP flow) — both
    can be enabled at once; this is not a replacement for it.

    Same gating pattern as the other card processors:
      1. WPAY_WP_ENABLED=true
      2. The shared WP site is configured (reuses onramp_wp's credentials —
         OnrampWPClient.configured() logic, checked via a plain client
         instantiation here to avoid duplicating the same-site auth check)
      3. source_domain is in WPAY_WP_STORES allowlist, OR country is in
         WPAY_WP_COUNTRIES allowlist (either one is sufficient — lets a
         store qualify by exact domain or by country without needing both)
    """
    if not bool(getattr(settings, "WPAY_WP_ENABLED", False)):
        return False

    from services.wpay_wp import WPayWPClient
    if not WPayWPClient().configured():
        return False

    raw = (getattr(settings, "WPAY_WP_STORES", "") or "").strip()
    if raw == "*":
        return True
    if raw != "":
        allowlist = {s.strip().lower() for s in raw.split(",") if s.strip()}
        if (source_domain or "").strip().lower() in allowlist:
            return True

    countries_raw = (getattr(settings, "WPAY_WP_COUNTRIES", "") or "").strip()
    if countries_raw:
        countries_allowlist = {c.strip().upper() for c in countries_raw.split(",") if c.strip()}
        if (country or "").strip().upper() in countries_allowlist:
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
from routes.customer import router as customer_router
app.include_router(customer_router)

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
            "interacDiscount": 5.0,
            "cryptoDiscount":  10.0,
        }

    return JSONResponse(config)


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

    # If this v2 store has a country pinned in data/checkout_v2_stores.txt
    # (lines like `mystore.com:US`), the file ALWAYS wins — protects against
    # a theme bridge that forgot to send `&country=US`. v1/non-listed stores
    # keep the query-param-driven behavior.
    _src_for_country = (
        request.query_params.get("source")
        or request.headers.get("host", "")
    )
    pinned = _v2_store_country(_src_for_country)
    if pinned:
        country = pinned

    currency = "USD" if country == "US" else "CAD"

    # Source store determines per-method gating. Per-store overrides come
    # from STORE_CONFIG_CSV (a CSV file). If a source has no row, fall back
    # to the global env defaults — preserves current behavior for stores
    # not yet in the file.
    source_domain = request.query_params.get("source", "")

    from services import store_config as _store_cfg

    # Defaults pulled from the existing env machinery — same as before.
    _env_card    = _is_card_enabled_for_source(source_domain)
    _env_altcoin = (
        bool(getattr(settings, "ALTCOIN_ENABLED", True))
        and bool(settings.NOWPAYMENTS_API_KEY)
    )
    # Onramp routes through the WP plugin path. The CSV `onramp` column
    # controls per-store visibility.
    _env_onramp = (
        bool(getattr(settings, "ONRAMP_WP_ENABLED", False))
        and bool(getattr(settings, "ONRAMP_WP_URL", ""))
        and (
            bool(getattr(settings, "ONRAMP_WP_CONSUMER_KEY", ""))
            or bool(getattr(settings, "ONRAMP_WP_APP_PASSWORD", ""))
        )
    )

    # CSV per-store overrides (None = no override, fall back to default)
    card_enabled    = _store_cfg.is_enabled(source_domain, "card",    _env_card)
    # Card method disabled on US AND CA stores.
    #   - US: disabled per business decision (e.g. processor switch / risk)
    #   - CA: pymtz integration was originally US-only — never had a CA path
    # To re-enable for a country, remove that branch.
    if country in ("US", "CA"):
        card_enabled = False

    altcoin_enabled = _store_cfg.is_enabled(source_domain, "altcoin", _env_altcoin)
    onramp_enabled  = _store_cfg.is_enabled(source_domain, "onramp",  _env_onramp)

    # Hard kill onramp for US stores — onramp providers (Transak/MoonPay) have
    # poor US support, and the WP-plugin path bills in USD via CAD-cloaked
    # merchant. Disabling globally avoids per-store CSV bookkeeping.
    if country == "US":
        onramp_enabled = False

    # Bitcoin (BTCPay) country allowlist. Same "hard kill by country" pattern
    # as onramp above — every order is either US or CA, so this gates the
    # option per country without per-store CSV bookkeeping. The matching
    # server-side guard lives in routes/checkout.py::checkout_crypto, so a
    # customer in a non-allowed country can't reach it by POSTing directly
    # either. Keep both lists in sync when changing availability.
    # To offer it in another country, add that code to the tuple below.
    crypto_enabled = country in ("CA", "US")

    ctx = {
        "store_name": (
            request.query_params.get("storename") + " Checkout"
            if request.query_params.get("storename")
            else (brand.store_name if brand else "Checkout")
        ),
        "logo_url":         brand.logo_url          if brand else "",
        "header_bg_url":    brand.header_bg_url     if brand else "",
        **dict(zip(("accent_color", "accent_hover"), _resolve_accent(request, brand))),
        "interac_email":    brand.interac_email     if brand else settings.INTERAC_DEFAULT_EMAIL,
        "zelle_email":      settings.ZELLE_DEFAULT_EMAIL,
        "interac_discount": float(brand.interac_discount if brand else 10),
        "zelle_discount":   float(getattr(brand, "zelle_discount", None) or 10),
        "crypto_discount":  float(brand.crypto_discount  if brand else 10),
        "store_country":    country,
        "store_currency":   currency,
        "base_url":         settings.BASE_URL,
        "source_domain":    source_domain,
        "card_enabled":      card_enabled,
        "altcoin_enabled":   altcoin_enabled,
        # Bitcoin (BTCPay) — Canada-only (see the country gate above).
        "crypto_enabled":    crypto_enabled,
        "onramp_wp_enabled": onramp_enabled,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY or "",
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
        # Stripe direct — its own card rail. Publishable key is safe to
        # expose to the browser; secret key stays server-side.
        "stripe_direct_enabled":     _stripe_direct_enabled_for(source_domain),
        # stripe_publishable_key already in ctx above (legacy bridge uses same).
        # WPay Channels — hosted redirect, USD-only, $20 minimum enforced server-side.
        "wpay_enabled":              _wpay_enabled_for(source_domain),
        # WPay 2D via the WordPress plugin site (separate option from wpay_enabled above).
        "wpay_2d_enabled":           _wpay_2d_enabled_for(source_domain, country),
        # Shopify Processor — routes through the WP Shopify bridge. Requires
        # both the kill-switch AND the bridge URL/secret, since without those
        # the endpoint can only 503 (see checkout_shopifyprocessor) — showing
        # a payment option that can't complete is worse than hiding it.
        "shopify_processor_enabled": (
            bool(getattr(settings, "SHOPIFY_PROCESSOR_ENABLED", False))
            and bool(getattr(settings, "SHOPIFY_PROCESSOR_WP_URL", ""))
            and bool(getattr(settings, "SHOPIFY_PROCESSOR_SHARED_SECRET", ""))
        ),
    }

    # Template routing — opt-in via `?v=` query param.
    #   v=2      → checkout-v2.html      (US — sage/mint editorial)
    #   v=ca     → checkout-ca.html      (Canada — slate stone, oxblood accent)
    #   v=new-ca → checkout-new-ca.html  (Card/WPay 2D, Interac, and Bitcoin
    #              only — a smaller payment picker than checkout-ca.html.
    #              wpay_2d_enabled is forced True here regardless of the
    #              store's normal WPAY_WP_STORES/WPAY_WP_COUNTRIES allowlist —
    #              landing on this specific URL is itself the deliberate
    #              choice to offer WPay for this checkout.)
    #   else     → checkout.html        (legacy v1)
    # The peptide store theme appends the right `&v=` to the checkout URL.
    v_param = request.query_params.get("v", "").strip().lower()
    if v_param == "ca":
        try:
            template = jinja_env.get_template("checkout-ca.html")
        except Exception:
            template = jinja_env.get_template("checkout.html")
    elif v_param == "2":
        try:
            template = jinja_env.get_template("checkout-v2.html")
        except Exception:
            template = jinja_env.get_template("checkout.html")
    elif v_param == "new-ca":
        try:
            template = jinja_env.get_template("checkout-new-ca.html")
            ctx["wpay_2d_enabled"] = True
        except Exception:
            template = jinja_env.get_template("checkout.html")
    else:
        template = jinja_env.get_template("checkout.html")
    html = template.render(**ctx)
    response = HTMLResponse(content=html)

    # Visitor tracking (admin "Visits" tab) — a long-lived cookie
    # correlates this page load with whichever order it eventually
    # produces (order.visitor_id, set in routes/checkout.py). SameSite=Lax
    # (not Strict) because a visit almost always arrives via a cross-site
    # top-level navigation from the brand's storefront on a different
    # domain — Strict cookies are withheld on exactly that navigation.
    # Never let a hiccup here break the checkout page itself.
    try:
        visitor_id = request.cookies.get("cs_vid")
        is_new_visitor = visitor_id is None
        if is_new_visitor:
            visitor_id = secrets.token_hex(16)
            response.set_cookie(
                "cs_vid", visitor_id,
                httponly=True, secure=True, samesite="lax",
                max_age=60 * 60 * 24 * 90,
            )
        # Last-resort fallback — never a raw IP (a bot/scanner hitting the
        # server's IP directly instead of a real domain), since that
        # shows up in the admin dashboard's "Top Referring Store" ranking
        # as if it were an actual storefront.
        _host_fallback = request.headers.get("host", "")
        if _looks_like_ip(_host_fallback):
            _host_fallback = ""
        async with AsyncSessionLocal() as db:
            db.add(Visit(
                visitor_id=visitor_id,
                brand_id=brand.id if brand else None,
                store_name=brand.store_name if brand else None,
                source_domain=source_domain or _host_fallback,
                ip_address=_client_ip(request),
                # Cheap and synchronous — city/region are resolved lazily
                # on admin view instead (services/geoip.py), never here.
                # A live third-party geo call has no place in this
                # customer-facing hot path.
                country=request.headers.get("CF-IPCountry"),
                user_agent=request.headers.get("user-agent", ""),
                referrer=request.headers.get("referer"),
            ))
            await db.commit()
    except Exception as e:
        logger.error(f"Visit logging failed (checkout page still served): {e}")

    return response


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

    _acc, _hov = _resolve_accent(request, brand, "#dc2626", "#b91c1c")
    v_param = request.query_params.get("v", "").strip().lower()
    is_v2 = v_param == "2"
    is_ca = v_param == "ca"
    is_new_ca = v_param == "new-ca"
    ctx = {
        "store_name":        brand.store_name   if brand else "Checkout",
        "logo_url":          brand.logo_url     if brand else "",
        "accent_color":      _acc,
        "accent_hover":      _hov,
        "session_id":        session_id,
        "stripe_worker_url": settings.STRIPE_WORKER_URL,
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
        # v2 reskin flag — driven by ?v=2 (propagated from checkout-v2's
        # withBrandAccent). Used by the template to include the v2 stylesheet.
        "is_v2":             is_v2,
        "is_ca":             is_ca,
        # Country drives the v2 palette (US=sky-blue, CA=emerald). Propagated
        # from checkout-v2 via withBrandAccent; falls back to CA (matches the
        # checkout-page default).
        "store_country":     (request.query_params.get("country", "CA") or "CA").upper(),
    }

    # Template routing for /order/success — v=ca picks the CA design,
    # v=2 picks v2, v=new-ca picks the new-ca editorial design, anything
    # else falls back to v1.
    if is_new_ca:
        template_name = "order-success-new-ca.html"
    elif is_ca:
        template_name = "order-success-ca.html"
    elif is_v2:
        template_name = "order-success-v2.html"
    else:
        template_name = "order-success.html"
    try:
        template = jinja_env.get_template(template_name)
    except Exception:
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

    _acc, _hov = _resolve_accent(request, brand)
    _v_param = request.query_params.get("v", "").strip().lower()
    is_v2 = _v_param == "2"
    is_ca = _v_param == "ca"
    is_new_ca = _v_param == "new-ca"
    ctx = {
        "store_name":    order.store_name or (brand.store_name if brand else "Checkout"),
        "logo_url":      brand.logo_url   if brand else "",
        "accent_color":  _acc,
        "accent_hover":  _hov,
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
        # Read the percentage from the ORDER, not the brand — that way the
        # label always matches the discount_amount that was actually applied,
        # even for old orders placed before the brand's % was changed. Brand
        # value is only used as a fallback for orders missing discount_pct.
        "interac_discount_pct":    float(order.discount_pct or (brand.interac_discount if brand else 10)),
        "zelle_discount_pct":      float(order.discount_pct or (getattr(brand, "zelle_discount", None) or 10)),
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
        # v2 reskin flag — propagated from checkout-v2 via withBrandAccent.
        "is_v2":         is_v2,
        "is_ca":         is_ca,
        # Country drives the v2 palette. Propagated from checkout-v2 via
        # withBrandAccent (?country=US/CA). Falls back to inferring from the
        # order's currency (CAD → CA, anything else → US).
        "store_country": (
            (request.query_params.get("country") or "").upper()
            or ("CA" if (order.currency or "").upper() == "CAD" else "US")
        ),
    }

    # Confirmation template routing — picks the right variant by payment
    # method, then the right skin by `v` query param:
    #   v=new-ca → confirmation*-new-ca.html (editorial, Interac/Bitcoin only —
    #              no altcoin variant since checkout-new-ca.html doesn't offer it)
    #   v=ca     → confirmation*-ca.html  (warm cognac)
    #   v=2      → confirmation*-v2.html  (sage/mint)
    #   else     → confirmation*.html     (legacy v1)
    if pm_value == "crypto":
        new_ca_name, ca_name, v2_name, v1_name = "confirmation_crypto-new-ca.html", "confirmation_crypto-ca.html", "confirmation_crypto-v2.html", "confirmation_crypto.html"
    elif pm_value == "altcoin":
        new_ca_name, ca_name, v2_name, v1_name = "confirmation_altcoin-ca.html", "confirmation_altcoin-ca.html", "confirmation_altcoin-v2.html", "confirmation_altcoin.html"
    else:
        new_ca_name, ca_name, v2_name, v1_name = "confirmation-new-ca.html", "confirmation-ca.html", "confirmation-v2.html", "confirmation.html"

    if is_new_ca:
        try:
            template = jinja_env.get_template(new_ca_name)
        except Exception:
            template = jinja_env.get_template(v1_name)
    elif is_ca:
        try:
            template = jinja_env.get_template(ca_name)
        except Exception:
            template = jinja_env.get_template(v1_name)
    elif is_v2:
        try:
            template = jinja_env.get_template(v2_name)
        except Exception:
            template = jinja_env.get_template(v1_name)
    else:
        template = jinja_env.get_template(v1_name)
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.ENVIRONMENT}
