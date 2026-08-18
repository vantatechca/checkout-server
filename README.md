# Checkout Server

Multi-brand, multi-domain checkout server with Credit Card (Helcim), Interac e-Transfer, and Crypto (BTCPay + Boltz) payment support. Built with FastAPI + MariaDB + Celery.

---

## Project Structure

```
checkout-server/
├── main.py                        # FastAPI app + brand middleware
├── config.py                      # Settings (pydantic-settings + .env)
├── database.py                    # Async SQLAlchemy engine
├── requirements.txt
│
├── models/
│   ├── brand.py                   # Brand (per-domain config)
│   └── order.py                   # Order, OrderItem, InteracPayment, CryptoInvoice
│
├── routes/
│   ├── checkout.py                # POST /api/checkout/{card|interac|crypto}
│   ├── webhooks.py                # POST /webhooks/btcpay
│   └── admin.py                   # GET|POST /admin/orders|brands|interac
│
├── services/
│   ├── helcim.py                  # Helcim credit card API wrapper
│   ├── btcpay.py                  # BTCPay Server API wrapper + webhook verifier
│   ├── interac_watcher.py         # Gmail API polling for Interac e-Transfer matching
│   └── order_id.py                # ORD-XXXXXXXX generator
│
├── tasks/
│   └── celery_app.py              # Celery worker: Interac polling, order expiry, BTCPay fallback
│
├── templates/
│   ├── checkout.html              # Jinja2 template (brand-injected)
│   └── confirmation.html          # Order confirmation page
│
├── static/                        # CSS/JS/images (served by Nginx directly)
│
└── scripts/
    ├── schema.sql                 # MariaDB schema + seed data
    ├── nginx.conf                 # Multi-domain Nginx config
    ├── setup_vps.sh               # Full VPS bootstrap script
    ├── install_btcpay.sh          # BTCPay Docker install
    └── embed_example.js           # How Shopify stores link to this checkout
```

---

## Local Development (Windows)

### 1. Start the database + Redis
Requires Docker Desktop running.
```powershell
docker-compose up -d
```
This starts MariaDB on `3306` and Redis on `6379`, matching the defaults in `.env`.

### 2. Set up the Python environment
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configure `.env`
Copy `.env.example` to `.env` and fill in real values. Make sure `DB_PASSWORD` matches whatever password `docker-compose.yml` sets for `checkout_user` (or update one to match the other — MariaDB's user/password is only set on the *first* container init, so changing `docker-compose.yml` later won't retroactively change it; you'd need to `ALTER USER` on the running container instead).

### 4. Load the database schema
The base schema plus every one-off migration under `migrations/` need to run once, in order, on a fresh database:
```powershell
mysql -u checkout_user -p checkout_db < scripts/schema.sql
python -m migrations.add_company_research_columns
python -m migrations.add_customer_accounts_table
python -m migrations.add_image_url_to_order_items
python -m migrations.create_missing_tables
python -m migrations.sync_orders_table
python -m migrations.add_original_price_to_order_items
python -m migrations.add_received_amount_columns
python -m migrations.add_wpay_2d_to_payment_method_enum
```
All migration scripts are idempotent — safe to re-run. Check `migrations/` for any new ones added since this was written.

### 5. Run the server
```powershell
uvicorn main:app --reload
```
Comes up on `http://127.0.0.1:8000`.

### 6. (Optional) Celery worker + beat
Only needed to test async payment methods (Interac, Zelle, Crypto, WPay) resolving via their poll/webhook fallback rather than staying `pending` forever:
```powershell
celery -A tasks.celery_app worker --loglevel=info
celery -A tasks.celery_app beat --loglevel=info
```

### A note on `BASE_URL` and redirect-based payment methods
`.env`'s `BASE_URL` is used to build every redirect/callback URL sent to external processors (BTCPay, pymtz, WPay, etc.) — including when running locally. If it's still set to the production domain while you're testing locally, any redirect-based checkout will bounce you back to *production*, not your local server, and the confirmation page will 404 with "Order not found" since that order only exists in your local database.

To test a full redirect round-trip locally, temporarily set:
```
BASE_URL=http://127.0.0.1:8000
```
This only works if the browser completing checkout is on the same machine as the server (webhooks from a third-party processor still can't reach `127.0.0.1` — that needs a tunnel like ngrok, or testing on a real deployment instead). **Remember to switch `BASE_URL` back to the production domain before deploying** — don't let a local testing value leak into a production `.env`.

---

## Quick Start (VPS)

### 1. Upload project
```bash
scp -r checkout-server/ ubuntu@your-vps-ip:~/
```

### 2. Run setup script
```bash
ssh ubuntu@your-vps-ip
cd ~/checkout-server
sudo bash scripts/setup_vps.sh
```

### 3. Edit .env
```bash
nano .env
# Fill in: DB_PASSWORD, HELCIM_API_TOKEN, BTCPAY_*, GMAIL_WATCH_EMAIL, etc.
```

### 4. Gmail OAuth (Interac watcher)
```bash
source venv/bin/activate
python services/interac_watcher.py --setup
# Follow the browser OAuth flow
```

### 5. SSL certificates
```bash
sudo certbot --nginx -d checkout.store1.com -d checkout.store2.com
```

### 6. Start services
```bash
sudo systemctl start checkout-api checkout-worker checkout-beat
sudo systemctl status checkout-api   # verify running
```

### 7. Install BTCPay
```bash
sudo bash scripts/install_btcpay.sh
# Then configure store + API key, set BTCPAY_* in .env
```

---

## Adding a New Store Domain

1. Point DNS to your VPS IP (A record)
2. Add domain to Nginx `server_name` list in `/etc/nginx/sites-available/checkout`
3. Get SSL cert: `certbot --nginx -d checkout.newstore.com`
4. Insert brand row in DB:
```sql
INSERT INTO brands (domain, store_name, interac_email, accent_color, accent_hover)
VALUES ('checkout.newstore.com', 'New Store', 'pay@newstore.com', '#1565c0', '#0d47a1');
```
5. `sudo systemctl reload nginx` — done.

---

## Payment Flows

### Credit Card (Helcim)
```
Customer → HelcimPay.js tokenizes card → window.helcimPayToken set
→ POST /api/checkout/card (with helcim_pay_token)
→ Backend calls Helcim API → charges card
→ Order marked paid → redirect to /order/{id}/confirmation
```

### Interac e-Transfer
```
Customer → POST /api/checkout/interac
→ Order created (status: pending)
→ Customer shown instructions: send $X to {email}, note ORD-XXXXXXXX
→ Celery beat polls Gmail every 5 min
→ On match: order.payment_status = 'paid'
→ Confirmation page polls /api/checkout/status/{id} every 15s
```

### Crypto (BTCPay + Boltz)
```
Customer → POST /api/checkout/crypto
→ BTCPay invoice created
→ Customer redirected to BTCPay hosted page (coin selection, QR, timer)
→ BTCPay webhook → POST /webhooks/btcpay
→ Order marked paid
```

### WPay Channels (Hosted Payment Page)
```
Customer → POST /api/checkout/wpay
→ services/wpay.py POSTs to WPay's hpp/request.php (no card data — hosted page)
→ Customer redirected to WPay's hosted page
→ WPay webhook → POST /webhooks/wpay (cross-verified against WPay's own status API
   before trusting it — see routes/webhooks.py)
→ Order marked paid
→ Fallback: tasks/celery_app.py polls WPay's fetch_record API after 45 min
  in case the webhook never arrives
```
Gated per-store via `WPAY_ENABLED` / `WPAY_STORES` in `.env` — hidden entirely
until both are set. USD-only, $20 USD minimum enforced both client- and
server-side (`services/wpay.py:MIN_AMOUNT`).

**Status as of this writing:** the HPP integration above is fully built and
tested, but the specific WPay merchant account currently configured
(`WPAY_UID`) is provisioned for WPay's *other* integration method ("2D" —
direct card entry via a WooCommerce plugin + Basis Theory tokenization), not
HPP. Every real HPP request returns `{"status":"0","message":"Inactive
Merchant Terminal"}` until either HPP is separately enabled for this account,
or a 2D-based integration is stood up instead (in progress — see WPay's
`wpay-channels-gateway` WooCommerce plugin, being set up on a fresh WordPress
install for this purpose).

---

## Embedding from Shopify

Cart items are passed via `?items=<base64json>`. See `scripts/embed_example.js` for the Liquid/JS snippet to add to your Shopify theme.

---

## Admin Endpoints

| Endpoint | Description |
|---|---|
| `GET /admin/orders` | List orders (filter by status, method, brand, email) |
| `GET /admin/orders/{id}` | Order detail with line items |
| `POST /admin/orders/{id}/mark-paid` | Manually mark order paid |
| `GET /admin/interac/unmatched` | Interac emails that couldn't auto-match |
| `POST /admin/interac/match` | Manually link Interac payment to order |
| `GET /admin/brands` | List brands |
| `POST /admin/brands` | Create brand |
| `PUT /admin/brands/{id}` | Update brand |

**Restrict `/admin/` to your IP** in `nginx.conf` (uncomment the `allow`/`deny` lines).

---

## Service Management

```bash
# Status
sudo systemctl status checkout-api checkout-worker checkout-beat

# Restart after .env changes
sudo systemctl restart checkout-api checkout-worker checkout-beat

# View logs
journalctl -u checkout-api -f
journalctl -u checkout-worker -f

# Reload Nginx
sudo nginx -t && sudo systemctl reload nginx
```
