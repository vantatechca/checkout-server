# WPay 2D (Direct Card) Integration — Setup Documentation

Last updated: 2026-07-28

## Overview

WPay 2D is a credit/debit card payment option processed through a WordPress +
WooCommerce site running the WPay Channels plugin. The checkout server
creates a WooCommerce order via the WC REST API, redirects the customer to
that order's WooCommerce pay-for-order page (WPay Channels' own card form,
tokenized via Basis Theory — raw card data never touches the checkout
server), and WooCommerce fires a webhook back to the checkout server when
the order's status changes.

This is a **separate integration** from `services/wpay.py` (the WPay HPP
flow, gated by `WPAY_ENABLED`/`WPAY_UID`/`WPAY_USER_TOKEN`) — both can be
enabled at once.

## Architecture

```
Customer → checkout-server (pepscheckoutportal.com)
  → POST /api/checkout/wpay_2d
  → creates local Order row (payment_method = wpay_2d)
  → creates WooCommerce order via REST API on wpay.pepscheckoutportal.com
  → redirects customer to WC's pay-for-order page
  → customer enters card on WPay Channels' Basis-Theory-tokenized form
  → WooCommerce fires webhook → POST /webhooks/wpay_2d
  → checkout-server marks local order paid/failed, creates Shopify order,
    fires affiliate webhook
```

Fallback: none currently implemented as a Celery task for wpay_2d specifically
(unlike the HPP flow's 45-min poll) — relies on the webhook firing reliably.

## Infrastructure

| Component | Value |
|---|---|
| Checkout server host | `dev1@nickcheckout` VPS, path `/srv/shared/checkout-server` |
| Checkout server systemd unit | `checkout.service` (single unit — no separate worker/beat units found on this box) |
| Checkout server public domain | `https://pepscheckoutportal.com` (Cloudflare-proxied) |
| WordPress/WooCommerce site path | `/var/www/wordpress-wpay` on the same VPS |
| WordPress site domain (current) | `https://wpay.pepscheckoutportal.com` — real HTTPS via Let's Encrypt (previously `http://202.181.177.119:8080`, no SSL) |
| WordPress DB | `wordpress_wpay` (MariaDB) |
| WordPress active theme | Custom theme **"WPay Checkout"** (built for this project — clean redesign of the pay-for-order page) |
| Server IP | `202.181.177.119` |

## DNS (Cloudflare — zone `pepscheckoutportal.com`)

| Type | Name | Content | Proxy status |
|---|---|---|---|
| A | `wpay` | `202.181.177.119` | **DNS only** (must stay unproxied — Cloudflare must not sit in front for cert renewal / origin auth to keep working as configured) |

## SSL Certificate

- Issued via **Let's Encrypt / certbot** (`certbot --nginx -d wpay.pepscheckoutportal.com`)
- Cert location: `/etc/letsencrypt/live/wpay.pepscheckoutportal.com/`
- Expires: **2026-10-26** — certbot has an automatic renewal task scheduled, no manual action expected, but worth spot-checking closer to the date.

## nginx configuration

Two server blocks exist for the WordPress site:
1. **Legacy**: `/etc/nginx/sites-available/wordpress-wpay` — `listen 8080;`, `server_name _;` — the original bare-IP access point. Still present, not removed.
2. **Current/real domain**: `/etc/nginx/sites-available/wpay-domain` — `listen 80/443;`, `server_name wpay.pepscheckoutportal.com;`, HTTPS auto-configured by certbot.

Both blocks include this critical line (required for WordPress Application Password auth to work — nginx does not forward the `Authorization` header to PHP-FPM by default):
```nginx
fastcgi_param HTTP_AUTHORIZATION $http_authorization;
```

## wp-config.php additions

Added to `/var/www/wordpress-wpay/wp-config.php` (above the "stop editing" line):
```php
define( 'WP_ENVIRONMENT_TYPE', 'local' );
define( 'WP_HOME', 'https://wpay.pepscheckoutportal.com' );
define( 'WP_SITEURL', 'https://wpay.pepscheckoutportal.com' );
```
- `WP_ENVIRONMENT_TYPE=local` was needed to unlock Application Passwords before real HTTPS existed (the feature requires SSL by default). No longer strictly necessary now that real HTTPS is live, but left in place — harmless.
- `WP_HOME`/`WP_SITEURL` override the DB-stored site URL so WordPress doesn't redirect requests on the new domain back to the old bare-IP:8080 address.

## Credentials

**Keep this section restricted — treat this whole file as sensitive.**

### WordPress admin (wp-admin login)
- URL: `https://wpay.pepscheckoutportal.com/wp-login.php`
- Username: `admin`
- Password: `McueXTqFsJEgnqen0c2Y`

### WordPress Application Password (API use only — not for the login form)
- User: `admin`
- Application Password: `3wPy Gm9B h9G5 eu2V WVq9 tv9j`
- Used by `services/wpay_wp.py` via `ONRAMP_WP_USERNAME` / `ONRAMP_WP_APP_PASSWORD` in `.env`.

### WooCommerce REST API keys (legacy — no longer the active auth path)
- `ck_7a7191a85b07a9a857ef14c3355a0fea90bcf242` / `cs_ea1305f7ba67ea7c9aec71514b05745feb468acf` — kept in `.env` as `ONRAMP_WP_CONSUMER_KEY`/`SECRET` for fallback, but the app currently authenticates via the Application Password above whenever both `ONRAMP_WP_USERNAME` and `ONRAMP_WP_APP_PASSWORD` are set.
- A second key (`ck_d793db3a4fbb0c208073e29dd953624af77903f3` / `cs_a453859719eda7a5d81225ed9606ce61a04c7258`) was also generated during debugging (admin, Read/Write) — not currently referenced anywhere, safe to revoke if cleaning up.

### WooCommerce webhook (`/webhooks/wpay_2d`)
- Name: `WPay 2D → checkout-server`
- Topic: `Order updated`
- Delivery URL: `https://pepscheckoutportal.com/webhooks/wpay_2d`
- Secret: `PepsCheckoutWooCommerce***` (matches `.env`'s `WPAY_WP_WEBHOOK_SECRET` — must stay identical in both places)

### WPay Channels merchant account (the actual payment processor)
- Backoffice URL: `https://backoffice.wpaychannels.com`
- UID: `WpayUID0202`
- Backoffice login password: `WpCrm)(&_$0202&`
- Used in WooCommerce → Settings → Payments → WPay 2D (Direct Card) gateway settings as the **UID** field. The **User token** field was set to the same value as a first attempt — confirm this is actually correct by checking inside the WPay backoffice portal for a dedicated API token if issues arise.

## `.env` configuration (checkout-server)

```
ONRAMP_WP_ENABLED=false
ONRAMP_WP_URL=https://wpay.pepscheckoutportal.com
ONRAMP_WP_CONSUMER_KEY=ck_7a7191a85b07a9a857ef14c3355a0fea90bcf242
ONRAMP_WP_CONSUMER_SECRET=cs_ea1305f7ba67ea7c9aec71514b05745feb468acf
ONRAMP_WP_USERNAME=admin
ONRAMP_WP_APP_PASSWORD=3wPy Gm9B h9G5 eu2V WVq9 tv9j
WPAY_WP_ENABLED=true
WPAY_WP_WEBHOOK_SECRET=PepsCheckoutWooCommerce***
WPAY_WP_STORES=victoriapeps.ca
```

Notes:
- `ONRAMP_WP_ENABLED=false` is correct/intentional — the *onramp_wp* integration itself stays disabled; only the WPay 2D gateway (which reuses this same site's URL/credentials) is active.
- `WPAY_WP_STORES` is a comma-separated allowlist of source domains that see the WPay 2D option on checkout. Currently only `victoriapeps.ca`.
- Minimum order amount: **$5 USD** (`services/wpay_wp.py:MIN_AMOUNT`), enforced both client-side expectation and server-side in `routes/checkout.py`.

## Database migration required

```bash
python -m migrations.add_wpay_2d_to_payment_method_enum
```
Widens the `orders.payment_method` ENUM column to accept `'wpay_2d'`. Idempotent — safe to re-run. Must be run once per environment/database (was missing on this VPS initially, causing a 500 on every checkout attempt until run).

## Frontend styling

- Custom WordPress theme "WPay Checkout" is active, built specifically for this payment-only site (no real storefront pages are used).
- Additional CSS applied via **Appearance → Customize → Additional CSS** — cleans up the pay-for-order page (centered card layout, styled order table, calmer guest-order notice, wider container to prevent the card/expiry/CVC fields from rendering cramped, emphasized Total row, trust signal below the Pay button).
- **Known open item**: the "Payment method: Credit / Debit Card" summary row on the pay-for-order page is still showing — the CSS selector guess to hide it didn't match this theme's actual markup. Needs the real class name (inspect-element) to fix precisely.

## Debugging history (chronological, condensed)

1. Fixed duplicate/conflicting `ONRAMP_WP_URL`/`CONSUMER_KEY`/`CONSUMER_SECRET` blocks in `.env` (one pointed at a dead site).
2. Ran the missing `add_wpay_2d_to_payment_method_enum` migration (fixed a 500 on every checkout attempt).
3. Patched `services/wpay_wp.py` to raise a proper error with response details instead of crashing on a non-JSON WooCommerce response.
4. Fixed the WooCommerce webhook secret mismatch (401 on delivery).
5. WooCommerce site was briefly in "Coming Soon" mode — switched to Live (ruled out as the actual blocker, but needed regardless).
6. Diagnosed that WooCommerce's REST API consumer-key/secret query-string auth does not work at all on this site (returns `woocommerce_rest_cannot_create`/`cannot_view` regardless of key permissions) — root cause never fully confirmed, but Application Password auth was the working path.
7. Fixed nginx to forward the `Authorization` header to PHP-FPM (`fastcgi_param HTTP_AUTHORIZATION $http_authorization;`) — required for Application Password auth to reach WordPress at all.
8. WordPress Application Passwords were disabled site-wide because the site had no HTTPS — set `WP_ENVIRONMENT_TYPE=local` in `wp-config.php` to unlock the feature despite no SSL (temporary workaround, later made moot by real HTTPS).
9. Generated a real Application Password, confirmed it authenticates.
10. Found the actual root cause of a stubborn empty-response bug: `ONRAMP_WP_USERNAME` had gone missing from `.env` entirely (not overridden — just absent), causing the code to silently fall back to the broken consumer-key/secret path, which also hit the wrong URL due to an httpx `params=` merge quirk when combined with a URL that already has a query string.
11. Confirmed real WooCommerce order creation end-to-end (`201 Created`, valid `payment_url`).
12. Placed a real test order through the actual checkout flow — redirected correctly, but the gateway was disabled with no merchant credentials configured in WooCommerce.
13. Found the real WPay merchant credentials (`WpayUID0202`) in the WPay support chat, configured the gateway, got the live card form rendering.
14. Migrated the WordPress site from a bare IP:port to a real HTTPS domain (`wpay.pepscheckoutportal.com`) via Cloudflare DNS + certbot, since customers shouldn't enter card data on an HTTP page.
15. Styled the pay-for-order page (custom theme + Additional CSS) for a cleaner customer-facing appearance.

## Still open / next steps

- [ ] Fix the "Payment method" row CSS selector once its real class is known.
- [ ] Confirm the WPay backoffice **User token** value is actually correct (vs. just reusing the login password) if any live payment fails.
- [ ] Full real end-to-end test on `victoriapeps.ca` checkout (place order → complete/decline card payment → confirm webhook marks local order paid/failed correctly → confirm Shopify order + affiliate webhook fire).
- [ ] Consider whether the legacy `:8080` nginx block and the old WooCommerce REST API keys should be cleaned up/revoked now that the HTTPS domain + Application Password path is the working one.
- [ ] `ONRAMP_WP_GATEWAY_ID` and `WPAY_WP_PRODUCT_ID` are both unset (using defaults / fee_lines) — fine as-is, but worth confirming intentional if a real product catalog integration is ever wanted.
