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
