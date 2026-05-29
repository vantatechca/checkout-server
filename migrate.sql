-- ============================================================================
-- checkout-server — full catch-up migration (idempotent, Postgres)
-- Safe to run multiple times. Run via:
--   psql $DATABASE_URL -f migrate.sql
-- ============================================================================

-- ─── 1. orders — missing columns ─────────────────────────────────────────────

ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS original_subtotal       NUMERIC(10,2),
  ADD COLUMN IF NOT EXISTS promo_discount_pct      NUMERIC(5,2)  DEFAULT 0,
  ADD COLUMN IF NOT EXISTS promo_discount_amount   NUMERIC(10,2) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_customer_email_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS customer_emails_sent    INTEGER       DEFAULT 0,
  ADD COLUMN IF NOT EXISTS first_name              VARCHAR(100);

-- payment_method enum: add missing values (zelle, altcoin)
-- Postgres doesn't support ALTER TYPE ... ADD VALUE inside a transaction
-- so we do it with a DO block that ignores "already exists" errors.
DO $$
BEGIN
  ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'zelle';
EXCEPTION WHEN others THEN NULL;
END $$;

DO $$
BEGIN
  ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'altcoin';
EXCEPTION WHEN others THEN NULL;
END $$;

-- payment_status enum: add missing value (cancelled)
DO $$
BEGIN
  ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'cancelled';
EXCEPTION WHEN others THEN NULL;
END $$;

-- ─── 2. order_items — missing column ─────────────────────────────────────────

ALTER TABLE order_items
  ADD COLUMN IF NOT EXISTS original_price NUMERIC(10,2);

-- ─── 3. interac_payments — missing columns + enum value ──────────────────────

ALTER TABLE interac_payments
  ADD COLUMN IF NOT EXISTS received_amount NUMERIC(10,2);

-- Add 'underpaid' to interac_status enum if it's a named type
DO $$
BEGIN
  ALTER TYPE interac_status ADD VALUE IF NOT EXISTS 'underpaid';
EXCEPTION WHEN others THEN NULL;
END $$;

-- ─── 4. crypto_invoices — missing columns ────────────────────────────────────

ALTER TABLE crypto_invoices
  ADD COLUMN IF NOT EXISTS received_fiat NUMERIC(10,2);

-- ─── 5. zelle_payments — full table ──────────────────────────────────────────

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'zelle_status') THEN
    CREATE TYPE zelle_status AS ENUM ('waiting','matched','unmatched','manual','underpaid');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS zelle_payments (
    id              SERIAL PRIMARY KEY,
    order_id        VARCHAR(20) UNIQUE REFERENCES orders(id),
    expected_amount NUMERIC(10,2) NOT NULL,
    received_amount NUMERIC(10,2),
    sender_name     VARCHAR(255),
    sender_email    VARCHAR(255),
    matched_at      TIMESTAMPTZ,
    raw_email_id    VARCHAR(255) UNIQUE,
    status          zelle_status DEFAULT 'waiting',
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ─── 6. nowpayments_invoices — full table ────────────────────────────────────

CREATE TABLE IF NOT EXISTS nowpayments_invoices (
    id            SERIAL PRIMARY KEY,
    order_id      VARCHAR(20) UNIQUE REFERENCES orders(id),
    np_invoice_id VARCHAR(255) UNIQUE NOT NULL,
    np_payment_id VARCHAR(255),
    invoice_url   TEXT,
    coin          VARCHAR(50),
    amount_fiat   NUMERIC(10,2) NOT NULL,
    received_fiat NUMERIC(10,2),
    status        VARCHAR(50) DEFAULT 'waiting',
    settled_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ─── 7. customer_email_log — full table ──────────────────────────────────────

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'email_log_type') THEN
    CREATE TYPE email_log_type AS ENUM ('reminder','underpaid');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS customer_email_log (
    id         SERIAL PRIMARY KEY,
    order_id   VARCHAR(20) NOT NULL,
    email_type email_log_type NOT NULL,
    sent_to    VARCHAR(255) NOT NULL,
    subject    VARCHAR(255) NOT NULL,
    body_text  TEXT,
    body_html  TEXT,
    sent_by    VARCHAR(100) DEFAULT 'admin',
    success    INTEGER DEFAULT 1,
    sent_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_customer_email_log_order_id
  ON customer_email_log(order_id);

-- ─── 8. brands — missing column ──────────────────────────────────────────────
-- The model has helcim_api_key on Brand but original schema may be missing it

ALTER TABLE brands
  ADD COLUMN IF NOT EXISTS helcim_api_key TEXT;

-- ─── Done ─────────────────────────────────────────────────────────────────────
SELECT 'Migration complete' AS status;