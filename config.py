from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_NAME: str = "checkout_db"
    DB_USER: str = "checkout_user"
    DB_PASSWORD: str = ""

    # App
    SECRET_KEY: str = "changeme"
    ENVIRONMENT: str = "production"
    BASE_URL: str = "https://checkout.yourdomain.com"

    # Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # Helcim
    HELCIM_API_TOKEN: str = ""
    HELCIM_API_URL: str = "https://api.helcim.com/v2"

    # BTCPay
    BTCPAY_URL: str = ""
    BTCPAY_API_KEY: str = ""
    BTCPAY_STORE_ID: str = ""
    BTCPAY_WEBHOOK_SECRET: str = ""

    RESEND_API_KEY: str = ""

    # Gmail
    GMAIL_CREDENTIALS_FILE: str = "./gmail_credentials.json"
    GMAIL_TOKEN_FILE: str = "./gmail_token.json"
    GMAIL_WATCH_EMAIL: str = ""

    # Interac / Zelle
    INTERAC_DEFAULT_EMAIL: str = ""
    ZELLE_DEFAULT_EMAIL: str = ""

    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

    BRIDGE_SECRET: str = ""
    BRIDGE_URL: str = "https://bridge-7.flystarcafe7.workers.dev/s2s"
    BRIDGE_SECRET_US: str = ""
    BRIDGE_URL_US:    str = ""

    SHOPIFY_STORE_DOMAIN: str = ""
    SHOPIFY_API_TOKEN: str = ""
    SHOPIFY_STORE_DOMAIN_US: str = ""
    SHOPIFY_API_TOKEN_US: str = ""
    SHOPIFY_WEBHOOK_SECRET: str = ""

    MPC_CHECKOUT_SHOP:  str = ""
    MPC_CHECKOUT_TOKEN: str = ""
    STORE_1_SHOP:       str = ""
    STORE_1_TOKEN:      str = ""

    MPC_WEBHOOK_SECRET:     str = ""
    STORE_1_WEBHOOK_SECRET: str = ""
    FROPEP_CHECKOUT_SHOP: str = ""
    FROPEP_CHECKOUT_TOKEN: str = ""
    FROPEP_WEBHOOK_SECRET: str = ""
    LUKPEP_CHECKOUT_SHOP: str = ""
    LUKPEP_CHECKOUT_TOKEN: str = ""
    LUKPEP_WEBHOOK_SECRET: str = ""
    TOPPEP_CHECKOUT_SHOP: str = ""
    TOPPEP_CHECKOUT_TOKEN: str = ""
    TOPPEP_WEBHOOK_SECRET: str = ""
    CANPEP_CHECKOUT_SHOP: str = ""
    CANPEP_CHECKOUT_TOKEN: str = ""
    CANPEP_WEBHOOK_SECRET: str = ""
    CRAPEP_CHECKOUT_SHOP: str = ""
    CRAPEP_CHECKOUT_TOKEN: str = ""
    CRAPEP_WEBHOOK_SECRET: str = ""
    SAKPEP_CHECKOUT_SHOP: str = ""
    SAKPEP_CHECKOUT_TOKEN: str = ""
    SAKPEP_WEBHOOK_SECRET: str = ""
    PLUPEP_CHECKOUT_SHOP: str = ""
    PLUPEP_CHECKOUT_TOKEN: str = ""
    PLUPEP_WEBHOOK_SECRET: str = ""
    LIPEP_CHECKOUT_SHOP: str = ""
    LIPEP_CHECKOUT_TOKEN: str = ""
    LIPEP_WEBHOOK_SECRET: str = ""
    MAXPEP_CHECKOUT_SHOP: str = ""
    MAXPEP_CHECKOUT_TOKEN: str = ""
    MAXPEP_WEBHOOK_SECRET: str = ""
    COLPEP_CHECKOUT_SHOP: str = ""
    COLPEP_CHECKOUT_TOKEN: str = ""
    COLPEP_WEBHOOK_SECRET: str = ""
    JAMPEP_CHECKOUT_SHOP: str = ""
    JAMPEP_CHECKOUT_TOKEN: str = ""
    JAMPEP_WEBHOOK_SECRET: str = ""
    SWOPEP_CHECKOUT_SHOP: str = ""
    SWOPEP_CHECKOUT_TOKEN: str = ""
    SWOPEP_WEBHOOK_SECRET: str = ""

    # NowPayments
    NOWPAYMENTS_API_KEY:     str = ""
    NOWPAYMENTS_IPN_SECRET:  str = ""
    NOWPAYMENTS_SUCCESS_URL: str = ""

    # Polling
    INTERAC_POLL_INTERVAL: int = 300

    # Order expiration
    ORDER_EXPIRY_CARD_MINUTES:    int = 60
    ORDER_EXPIRY_CRYPTO_MINUTES:  int = 60
    ORDER_EXPIRY_INTERAC_MINUTES: int = 2880

    # Affiliate dashboard
    AFFILIATE_DASHBOARD_URL: str = "https://peps-affiliate.onrender.com"

    # Card-enabled stores (comma-separated source domains)
    CARD_ENABLED_STORES: str = ""

    # Stripe — for embedded checkout in modal
    STRIPE_PUBLISHABLE_KEY: str = ""    # pk_test_... (test) or pk_live_... (live)
    STRIPE_WORKER_URL:      str = "https://stripe-worker.flystarcafe7.workers.dev"

    # Helcim — worker URL for thank-you page order lookup
    HELCIM_WORKER_URL:      str = "https://hc-worker.flystarcafe7.workers.dev"

    # pymtz — credit card via hosted payment page (replaces bridge card flow)
    PYMTZ_API_KEY:        str = ""   # pymtz_live_... (prod) or pymtz_test_... (test)
    PYMTZ_WEBHOOK_SECRET: str = ""   # whsec_... from POST /api/v1/webhooks

    # Lasso — cloaked CC checkout via Whop payment rails
    LASSO_STORE_ID:           str = ""   # data-store-id from your Lasso merchant dashboard
    LASSO_CHECKOUT_URL:       str = ""   # set to https://pepscheckoutportal.com/pay for proxy mode
    LASSO_REAL_CHECKOUT_URL:  str = ""   # actual Lasso URL e.g. https://checkout.yourdomain.com/checkout
    LASSO_WHOP_SECRET:        str = ""   # webhook signing secret from Whop dashboard (legacy — fallback for /webhooks/whop)

    # Whop — direct embedded checkout (parallel option to the existing Card flow).
    # Each order creates a one-time checkout configuration with a per-order plan
    # at the customer's actual cart total. The plan title is cloaked so peptide
    # names never reach Whop.
    #
    # Production credentials (whop.com dashboard)
    WHOP_API_KEY:        str = ""  # Company API key from Whop dashboard → Developer → API Keys
    WHOP_COMPANY_ID:     str = ""  # biz_xxxxxxxxxxxxx — the Whop company that owns the plan
    WHOP_PRODUCT_ID:     str = ""  # prod_xxxxxxxxxxxxx — existing product to attach plans to (so dashboard Product column populates)
    WHOP_WEBHOOK_SECRET: str = ""  # whsec_... or ws_... — Developer → Webhooks → Signing secret

    # Sandbox credentials (sandbox.whop.com — completely separate from prod).
    # Only used when WHOP_SANDBOX=true. Leave blank if not testing in sandbox.
    WHOP_SANDBOX_API_KEY:        str = ""
    WHOP_SANDBOX_COMPANY_ID:     str = ""
    WHOP_SANDBOX_PRODUCT_ID:     str = ""
    WHOP_SANDBOX_WEBHOOK_SECRET: str = ""

    # Shared settings (same for both environments)
    WHOP_CURRENCY:       str = "cad"  # lowercase ISO 4217 — must match what Whop supports for this company
    WHOP_PLAN_TITLE:     str = "DigiTech SecureSync"  # cloaked title shown to customer on Whop checkout
    WHOP_RETURN_URL:     str = ""  # optional — leave blank to NOT send redirect_url to Whop (skip-redirect handles it)
    WHOP_SANDBOX:        bool = False  # True → use WHOP_SANDBOX_* credentials and route API calls to sandbox-api.whop.com

    # Master kill-switch for the Card (WHOP) payment option. Set to False to
    # hide the option from the checkout page AND reject any direct API calls
    # to /api/checkout/whop-embed, without having to wipe API keys or limits.
    # Useful for quick on/off without touching credentials.
    WHOP_ENABLED:        bool = True

    # Volume cap — refuse new Whop checkouts once this CAD amount has been
    # routed through Whop today (UTC day). Counts both pending and paid
    # orders so a flood of abandoned attempts also throttles us. Set to 0
    # to disable. Recommended: start at 100 week 1, ramp to 300.
    WHOP_DAILY_LIMIT: float = 300.0

    # Optional email sink — if set, customer emails sent to Whop are
    # rewritten to {user}+{order_id}@{domain}. NOTE: not recommended at
    # volume (pattern detection flag). Leave empty to pass the customer's
    # real email through (Whop's send_customer_emails=false handles
    # email suppression on Whop's side).
    WHOP_SINK_EMAIL: str = ""

    # ── Tier plans (big risk reducer for production volume) ─────────────
    # Instead of creating a new inline plan per order (→ 600+ unique plans
    # per month, bot-like pattern), pre-create N fixed-price plans in the
    # Whop dashboard and route each cart to the closest one. From Whop's
    # POV the account now looks like a normal SaaS pricing ladder.
    #
    # Format: comma-separated "price:plan_id" pairs (price in major units).
    # Example: WHOP_TIER_PLANS=49:plan_aaa,99:plan_bbb,199:plan_ccc,299:plan_ddd
    # Leave empty to disable tiers and fall back to inline plan creation.
    WHOP_TIER_PLANS:         str = ""  # production tier plans
    WHOP_SANDBOX_TIER_PLANS: str = ""  # sandbox tier plans (different plan_ids)

    # How to map a cart total to a tier when tiers are configured:
    #   "round_down" — use the largest tier ≤ cart amount (customer pays
    #                  same or less than cart; you absorb any delta).
    #                  Safest UX — no surprise charges.
    #   "nearest"    — use whichever tier is closest (customer might pay
    #                  slightly more if cart is between two tiers).
    #   "round_up"   — use smallest tier ≥ cart amount (customer always
    #                  pays same or more). Margin gain but dispute risk.
    # If cart is outside the tier range (smaller than smallest or larger
    # than largest), falls back to inline plan creation for that one order.
    WHOP_TIER_STRATEGY: str = "round_down"

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "allow"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()