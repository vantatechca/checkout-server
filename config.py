from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    # DB_DIALECT picks the driver DATABASE_URL builds — "mysql" (default,
    # what the VPS/staging run — no .env change needed there) or "postgres"
    # (Render's managed Postgres, which this app has no other dependency
    # on; every column type used in models/ is dialect-agnostic SQLAlchemy).
    DB_DIALECT: str = "mysql"
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
    # Support inbox referenced in the "order received" email sent the moment
    # an Interac or Zelle order is placed — one shared address for every
    # store (same global-setting pattern as ZELLE_DEFAULT_EMAIL above), not
    # per-brand.
    ORDER_SUPPORT_EMAIL: str = ""

    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

    # View-only admin login — same dashboard, but every mutating endpoint
    # (mark paid, cancel, recover, match payments, edit brands, etc.) 403s.
    # Optional: leave both blank to disable the viewer login entirely.
    VIEWER_USERNAME: str = ""
    VIEWER_PASSWORD: str = ""

    BRIDGE_SECRET: str = ""

    BRIDGE_URL: str = "https://bridge-7.flystarcafe7.workers.dev/s2s"
    BRIDGE_SECRET_US: str = ""
    BRIDGE_URL_US:    str = ""

    SHOPIFY_PROCESSOR_WP_URL: str = ""

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
    # Master kill-switch for the "Altcoins" payment option (NowPayments). Set
    # to False to hide it from the checkout without wiping API keys.
    ALTCOIN_ENABLED:         bool = True

    # Polling
    INTERAC_POLL_INTERVAL: int = 300

    # Order expiration
    ORDER_EXPIRY_CARD_MINUTES:    int = 60
    ORDER_EXPIRY_CRYPTO_MINUTES:  int = 60
    ORDER_EXPIRY_INTERAC_MINUTES: int = 2880

    # Affiliate dashboard
    AFFILIATE_DASHBOARD_URL: str = "https://peps-affiliate.onrender.com"

    # Card-enabled stores (comma-separated source domains) — legacy fallback
    # when no per-store row exists in STORE_CONFIG_CSV.
    CARD_ENABLED_STORES: str = ""

    # Per-source-store payment-method config (CSV file). See
    # services/store_config.py for format details. If the file doesn't
    # exist, gating falls back to the global env flags below.
    STORE_CONFIG_CSV: str = "data/source_stores.csv"

    # Stores that should be served the V2 checkout template
    # (templates/checkout-v2.html). One source domain per line in the file;
    # blank lines and # comments are ignored. Missing file = no v2 stores.
    CHECKOUT_V2_STORES_FILE: str = "data/checkout_v2_stores.txt"

    # Stripe — for embedded checkout in modal
    # pk_test_... (test) or pk_live_... (live)
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WORKER_URL:      str = "https://stripe-worker.flystarcafe7.workers.dev"

    # Helcim — worker URL for thank-you page order lookup
    HELCIM_WORKER_URL:      str = "https://hc-worker.flystarcafe7.workers.dev"

    # pymtz — credit card via hosted payment page (replaces bridge card flow).
    # Two merchant accounts: one for Canada (charged in USD via the 1.38
    # conversion shown on checkout) and one for the US. The country is
    # selected from order.currency: CAD→CA account, USD→US account. The legacy
    # single-account keys below are kept as fallbacks so a partially-configured
    # deploy still works.
    PYMTZ_API_KEY:        str = ""   # legacy / fallback if per-country unset
    PYMTZ_WEBHOOK_SECRET: str = ""   # legacy / fallback if per-country unset
    PYMTZ_API_KEY_CA:        str = ""   # pymtz_live_... for the Canada account
    PYMTZ_WEBHOOK_SECRET_CA: str = ""   # whsec_... for the Canada account
    PYMTZ_API_KEY_US:        str = ""   # pymtz_live_... for the US account
    PYMTZ_WEBHOOK_SECRET_US: str = ""   # whsec_... for the US account

    # Shippo — shipping label purchase (admin dashboard "Shipping" tab). One
    # API token (not per-region — Shippo is a single account with multiple
    # sender addresses), but two separate ship-from addresses selected by
    # order.currency (CAD→CA, USD→US), same selection rule already used for
    # pymtz above. Domestic shipments only — no customs declaration is ever
    # built, so no package-contents description is ever sent to Shippo or
    # the carrier (see services/shippo.py for why).
    SHIPPO_ENABLED:   bool = False
    SHIPPO_API_TOKEN: str = ""   # shippo_live_... — from the Shippo dashboard

    SHIPPO_FROM_NAME_CA:     str = ""
    SHIPPO_FROM_ADDRESS1_CA: str = ""
    SHIPPO_FROM_ADDRESS2_CA: str = ""
    SHIPPO_FROM_CITY_CA:     str = ""
    SHIPPO_FROM_PROVINCE_CA: str = ""
    SHIPPO_FROM_POSTAL_CA:   str = ""
    SHIPPO_FROM_PHONE_CA:    str = ""

    SHIPPO_FROM_NAME_US:     str = ""
    SHIPPO_FROM_ADDRESS1_US: str = ""
    SHIPPO_FROM_ADDRESS2_US: str = ""
    SHIPPO_FROM_CITY_US:     str = ""
    SHIPPO_FROM_STATE_US:    str = ""
    SHIPPO_FROM_ZIP_US:      str = ""
    SHIPPO_FROM_PHONE_US:    str = ""

    # Default parcel size — most shipments are the same product in the same
    # packaging, so prefill these too (still fully editable per-order) same
    # as the ship-from address above. Weight in oz to match the rest of the
    # parcel fields (Shippo's API itself is unit-agnostic; oz was just this
    # codebase's existing convention).
    SHIPPO_DEFAULT_WEIGHT_OZ: str = ""
    SHIPPO_DEFAULT_LENGTH_IN: str = ""
    SHIPPO_DEFAULT_WIDTH_IN:  str = ""
    SHIPPO_DEFAULT_HEIGHT_IN: str = ""

    # Onramp via WordPress + 2530gateway plugin.
    # See services/onramp_wp.py for the architecture overview.
    # master kill-switch — flip to true to show the option
    ONRAMP_WP_ENABLED:         bool = False
    # e.g. http://23.137.251.62:8083 (no trailing /)
    ONRAMP_WP_URL:             str = ""
    # ck_... from WC → Settings → Advanced → REST API (HTTPS only)
    ONRAMP_WP_CONSUMER_KEY:    str = ""
    ONRAMP_WP_CONSUMER_SECRET: str = ""   # cs_...
    # Application Password auth — preferred over WC REST keys when the
    # site is HTTP. Create at WP admin → Users → admin → Application Passwords.
    ONRAMP_WP_USERNAME:        str = ""   # WP username (e.g. "admin")
    ONRAMP_WP_APP_PASSWORD:    str = ""   # 24-char app password "aBcD eFgH ..."
    # leave blank to use fee_lines (no product needed)
    ONRAMP_WP_PRODUCT_ID:      str = ""
    # leave blank for the default hosted gateway
    ONRAMP_WP_GATEWAY_ID:      str = ""
    ONRAMP_WP_WEBHOOK_SECRET:  str = ""   # signing secret from the WC webhook config

    # WPay 2D (Direct Card) via the same WordPress + WooCommerce site as
    # ONRAMP_WP_* above — reuses that site's URL/REST-API credentials
    # (ONRAMP_WP_URL, ONRAMP_WP_CONSUMER_KEY/SECRET or ONRAMP_WP_USERNAME/
    # APP_PASSWORD), since the WPay Channels plugin is installed there
    # alongside the 2530gateway one. See services/wpay_wp.py.
    # Separate option from WPAY_ENABLED (the HPP flow) — both can be shown
    # at once; this is NOT a replacement for it.
    # master kill-switch — flip to true to show the option
    WPAY_WP_ENABLED:        bool = False
    # WooCommerce gateway ID the plugin registers ($this->id in class-wpay-2d-gateway.php)
    WPAY_WP_GATEWAY_ID:     str = "wpay_2d"
    # leave blank to use fee_lines (no product needed)
    WPAY_WP_PRODUCT_ID:     str = ""
    # signing secret from the WC webhook config (can differ from ONRAMP_WP_WEBHOOK_SECRET)
    WPAY_WP_WEBHOOK_SECRET: str = ""
    # Per-store allowlist:
    #   ""    → no stores see the option
    #   "*"   → all stores
    #   "..." → only listed domains
    WPAY_WP_STORES:         str = ""
    # Per-country allowlist — a store qualifies if its domain is in
    # WPAY_WP_STORES OR its country is in this list (either is sufficient).
    # Lets new stores in an approved country get the option automatically
    # without editing WPAY_WP_STORES for each new store launch.
    #   ""        → no countries auto-qualify (domain list above still applies)
    #   "US,CA"   → any store whose country is US or CA qualifies
    WPAY_WP_COUNTRIES:      str = ""
    # Per WPay's compliance notice (2026-08-20, "Wpay Channels - Fully
    # Restored"): only these BILLING countries are approved for 2D/3DS —
    # anything else gets force-declined on WPay's end regardless, so this
    # rejects it here first for a clearer customer-facing error instead of
    # burning a WooCommerce order on a transaction that can't succeed.
    # NOT the same list as WPAY_WP_COUNTRIES above (that's which STORES can
    # show the option; this is which CUSTOMER billing countries are allowed
    # per transaction). Comma-separated ISO-2 codes; update here if WPay
    # revises the approved list.
    WPAY_WP_ALLOWED_GEOS:   str = "US,GB,CA,IN,AU,FR,HK,DE,JP,MX,SG,TR"

    # Stripe direct card processor — its own rail, not a fallback.
    # Per team architecture: customer card → Stripe (settles to its own
    # bank) → manual transfer to Grey wallet.
    #
    # STRIPE_PUBLISHABLE_KEY is defined elsewhere in this Settings class
    # (used by the legacy Stripe Elements bridge path). For Stripe Direct,
    # we reuse it on the frontend.
    STRIPE_DIRECT_ENABLED:                bool = False
    # sk_live_xxx or sk_test_xxx — server-side, never exposed
    STRIPE_SECRET_KEY:                    str = ""
    # whsec_xxx — for /webhooks/stripe_direct HMAC verification
    STRIPE_WEBHOOK_SECRET:                str = ""
    # appended to merchant name on bank statement; neutral text only (no pharma references)
    STRIPE_STATEMENT_DESCRIPTOR_SUFFIX:   str = ""
    # Per-store allowlist:
    #   ""    → no stores see Stripe option
    #   "*"   → all stores
    #   "..." → only listed domains
    STRIPE_DIRECT_STORES:                 str = ""

    # WPay Channels — hosted-redirect card processor (HPP flow). Customer is
    # redirected to WPay's own hosted page to enter card details; we never
    # see raw card data. No webhook signing secret — WPay doesn't document
    # HMAC callbacks, so /webhooks/wpay cross-verifies against WPay's own
    # status API instead (same approach used for pymtz).
    WPAY_ENABLED:    bool = False
    WPAY_UID:        str = ""   # merchant UID from WPay backoffice
    WPAY_USER_TOKEN: str = ""   # API user token from WPay backoffice
    # Per-store allowlist:
    #   ""    → no stores see the option
    #   "*"   → all stores
    #   "..." → only listed domains
    WPAY_STORES:     str = ""

    @property
    def DATABASE_URL(self) -> str:
        # DB_URL, if set, is a full connection string (Render/Neon convention)
        # — normalize its scheme to the async driver for whichever dialect
        # it declares, rather than assuming one.
        url = (getattr(self, "DB_URL", "") or "").strip()
        if url:
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)
            if url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+asyncpg://", 1)
            if url.startswith("mysql://"):
                return url.replace("mysql://", "mysql+aiomysql://", 1)
            return url
        if self.DB_DIALECT == "postgres":
            return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        # Sync driver — used by Alembic/any blocking tooling, not the app itself.
        url = (getattr(self, "DB_URL", "") or "").strip()
        if url:
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)
            if url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+psycopg2://", 1)
            if url.startswith("mysql://"):
                return url.replace("mysql://", "mysql+pymysql://", 1)
            return url
        if self.DB_DIALECT == "postgres":
            return f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "allow"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
