-- ============================================================================
-- Align the Neon (MySQL->PG migrated) schema with the app's SQLAlchemy models.
--
-- The DB was loaded from the MySQL-style schema, so booleans came across as
-- smallint and enum columns as text/varchar. The PG enum TYPES already exist
-- (paymentmethod, paymentstatus, interac_status, zelle_status, email_log_type)
-- and every stored value is a valid label, so these USING casts are safe.
--
-- Run against a Neon BRANCH first, verify the app boots + admin loads, then
-- apply to the main branch. Wrapped in a transaction: all-or-nothing.
--
--   docker exec -i checkout-pg psql "<neon-url>" -v ON_ERROR_STOP=1 < scripts/align_neon_schema.sql
-- ============================================================================
BEGIN;

-- brands.active : smallint(0/1) -> boolean
ALTER TABLE brands ALTER COLUMN active DROP DEFAULT;
ALTER TABLE brands ALTER COLUMN active TYPE boolean USING (active <> 0);
ALTER TABLE brands ALTER COLUMN active SET DEFAULT true;

-- orders.payment_method : text -> paymentmethod (no model default)
ALTER TABLE orders ALTER COLUMN payment_method TYPE paymentmethod
    USING payment_method::paymentmethod;

-- orders.payment_status : text -> paymentstatus (model default 'pending')
ALTER TABLE orders ALTER COLUMN payment_status DROP DEFAULT;
ALTER TABLE orders ALTER COLUMN payment_status TYPE paymentstatus
    USING payment_status::paymentstatus;
ALTER TABLE orders ALTER COLUMN payment_status SET DEFAULT 'pending';

-- interac_payments.status : text -> interac_status (model default 'waiting')
ALTER TABLE interac_payments ALTER COLUMN status DROP DEFAULT;
ALTER TABLE interac_payments ALTER COLUMN status TYPE interac_status
    USING status::interac_status;
ALTER TABLE interac_payments ALTER COLUMN status SET DEFAULT 'waiting';

-- zelle_payments.status : text -> zelle_status (model default 'waiting')
ALTER TABLE zelle_payments ALTER COLUMN status DROP DEFAULT;
ALTER TABLE zelle_payments ALTER COLUMN status TYPE zelle_status
    USING status::zelle_status;
ALTER TABLE zelle_payments ALTER COLUMN status SET DEFAULT 'waiting';

-- customer_email_log.email_type : text -> email_log_type (no model default)
ALTER TABLE customer_email_log ALTER COLUMN email_type TYPE email_log_type
    USING email_type::email_log_type;

-- NOTE: crypto_invoices.status and nowpayments_invoices.status stay VARCHAR --
-- those are String(50) in the models, NOT enums. Timestamps are left as-is
-- (timestamp-without-tz doesn't error against the ORM; only a cosmetic diff).

COMMIT;
