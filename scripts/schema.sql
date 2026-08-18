-- ============================================================
-- Checkout Server — MariaDB Schema
-- Run: mysql -u root -p checkout_db < scripts/schema.sql
-- ============================================================

CREATE DATABASE IF NOT EXISTS checkout_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE checkout_db;

-- ─── brands ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brands (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    domain           VARCHAR(255) NOT NULL UNIQUE,
    store_name       VARCHAR(255) NOT NULL,
    logo_url         TEXT,
    header_bg_url    TEXT,
    accent_color     VARCHAR(20)  DEFAULT '#dd1d1d',
    accent_hover     VARCHAR(20)  DEFAULT '#b01515',
    interac_email    VARCHAR(255),
    interac_discount DECIMAL(5,2) DEFAULT 5.00,
    crypto_discount  DECIMAL(5,2) DEFAULT 10.00,
    helcim_api_key   TEXT,
    btcpay_store_id  VARCHAR(255),
    allowed_origins  TEXT,
    active           TINYINT(1)   DEFAULT 1,
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_domain (domain),
    INDEX idx_active (active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── orders ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id              VARCHAR(20)  NOT NULL PRIMARY KEY,
    brand_id        INT          NOT NULL,
    store_name      VARCHAR(255) NOT NULL,

    -- Customer
    email           VARCHAR(255) NOT NULL,
    first_name      VARCHAR(100),
    last_name       VARCHAR(100) NOT NULL,
    phone           VARCHAR(50),

    -- Shipping
    address1        VARCHAR(255),
    address2        VARCHAR(255),
    city            VARCHAR(100),
    province        VARCHAR(100),
    postal_code     VARCHAR(20),
    country         CHAR(2)      DEFAULT 'CA',

    -- Billing
    bill_same       CHAR(1)      DEFAULT '1',
    bill_address1   VARCHAR(255),
    bill_address2   VARCHAR(255),
    bill_city       VARCHAR(100),
    bill_province   VARCHAR(100),
    bill_postal     VARCHAR(20),
    bill_country    CHAR(2),

    -- Financials
    subtotal        DECIMAL(10,2) NOT NULL,
    discount_pct    DECIMAL(5,2)  DEFAULT 0.00,
    discount_amount DECIMAL(10,2) DEFAULT 0.00,
    total           DECIMAL(10,2) NOT NULL,
    currency        CHAR(3)       DEFAULT 'CAD',

    -- Payment
    payment_method  ENUM('card','interac','crypto') NOT NULL,
    payment_status  ENUM('pending','paid','failed','refunded','expired','manual') DEFAULT 'pending',
    payment_ref     VARCHAR(255),
    payment_notes   TEXT,
    paid_at         DATETIME,

    -- Meta
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    source_domain   VARCHAR(255),

    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_order_brand FOREIGN KEY (brand_id) REFERENCES brands(id),
    INDEX idx_email       (email),
    INDEX idx_brand_status (brand_id, payment_status),
    INDEX idx_status      (payment_status),
    INDEX idx_method      (payment_method),
    INDEX idx_created     (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── order_items ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_items (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    order_id    VARCHAR(20) NOT NULL,
    product_id  VARCHAR(255),
    title       VARCHAR(255) NOT NULL,
    variant     VARCHAR(255),
    qty         INT          DEFAULT 1,
    price       DECIMAL(10,2) NOT NULL,
    total       DECIMAL(10,2) NOT NULL,

    CONSTRAINT fk_item_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    INDEX idx_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── interac_payments ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS interac_payments (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    order_id        VARCHAR(20),
    expected_amount DECIMAL(10,2) NOT NULL,
    sender_name     VARCHAR(255),
    sender_email    VARCHAR(255),
    matched_at      DATETIME,
    raw_email_id    VARCHAR(255) UNIQUE,  -- Gmail message ID (dedup)
    status          ENUM('waiting','matched','unmatched','manual') DEFAULT 'waiting',
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_interac_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    INDEX idx_status     (status),
    INDEX idx_order      (order_id),
    INDEX idx_email_id   (raw_email_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── crypto_invoices ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_invoices (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    order_id            VARCHAR(20) UNIQUE,
    btcpay_invoice_id   VARCHAR(255) NOT NULL UNIQUE,
    btcpay_invoice_url  TEXT,
    coin                VARCHAR(20),
    amount_crypto       DECIMAL(20,8),
    amount_fiat         DECIMAL(10,2) NOT NULL,
    status              VARCHAR(50)   DEFAULT 'New',
    expires_at          DATETIME,
    settled_at          DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_crypto_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    INDEX idx_btcpay_id (btcpay_invoice_id),
    INDEX idx_status    (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- Seed: example brand entries
-- Replace with your real domains and config.
-- ============================================================

INSERT IGNORE INTO brands
  (domain, store_name, accent_color, accent_hover, interac_email, interac_discount, crypto_discount)
VALUES
  ('checkout.store1.com', 'Store One Research',     '#dd1d1d', '#b01515', 'pay@store1.com', 5.00, 10.00),
  ('checkout.store2.com', 'Store Two Research',     '#1565c0', '#0d47a1', 'pay@store2.com', 5.00, 10.00),
  ('checkout.store3.com', 'Store Three Peptides',   '#2e7d32', '#1b5e20', 'pay@store3.com', 5.00,  8.00),
  ('localhost',           'Dev Store (Local)',       '#dd1d1d', '#b01515', 'dev@localhost',   5.00, 10.00);


-- ============================================================
-- Useful views for admin dashboard
-- ============================================================

CREATE OR REPLACE VIEW v_orders_summary AS
SELECT
    o.id,
    o.store_name,
    o.email,
    CONCAT(COALESCE(o.first_name,''), ' ', o.last_name) AS customer_name,
    o.payment_method,
    o.payment_status,
    o.total,
    o.currency,
    o.paid_at,
    o.created_at,
    b.domain
FROM orders o
LEFT JOIN brands b ON o.brand_id = b.id
ORDER BY o.created_at DESC;

CREATE OR REPLACE VIEW v_revenue_by_store AS
SELECT
    store_name,
    payment_method,
    COUNT(*)              AS order_count,
    SUM(total)            AS gross_revenue,
    SUM(discount_amount)  AS total_discounts,
    SUM(CASE WHEN payment_status='paid' THEN total ELSE 0 END) AS collected_revenue
FROM orders
GROUP BY store_name, payment_method
ORDER BY gross_revenue DESC;

CREATE OR REPLACE VIEW v_interac_unmatched AS
SELECT
    ip.id,
    ip.order_id,
    ip.expected_amount,
    ip.sender_email,
    ip.notes,
    ip.created_at
FROM interac_payments ip
WHERE ip.status = 'unmatched'
ORDER BY ip.created_at DESC;
