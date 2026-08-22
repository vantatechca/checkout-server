# Render standby deployment

A second, independent copy of checkout-server on Render — usable if the VPS
goes down. This is **not** a live mirror of the VPS: it runs its own
database, starting empty. Read "What this actually gives you" below before
relying on it during a real outage.

## Architecture

| | VPS (primary) | Render (standby) |
|---|---|---|
| App | systemd, `checkout.service` | Render Web Service (`checkout-web`) |
| Background tasks | systemd, `checkout-worker.service` | Render Background Worker (`checkout-worker`) |
| Database | MariaDB, local to the VPS | MariaDB, a Render Private Service (`checkout-mariadb`) with its own disk |
| Redis | local to the VPS | Render Key Value (`checkout-redis`) |
| Data | real production orders | **separate, starts empty** |

The two databases are **not synced or replicated** — this is deliberately
the simplest version that works, not a hot-standby with live data. See
"Keeping data in sync" below for what to do about that.

## One-time setup

1. **Push this repo to GitHub** (if not already there) — Render deploys by
   connecting to a GitHub repo, not the VPS's bare git repo.
2. In the Render dashboard: **New +** → **Blueprint** → connect the repo.
   Render finds `render.yaml` at the repo root automatically and shows you
   the 4 services it's about to create (`checkout-mariadb`, `checkout-redis`,
   `checkout-web`, `checkout-worker`). Click **Apply**.
3. Render will prompt for every `sync: false` variable in `render.yaml`
   during this flow — fill in what you have (see the checklist below);
   leave the rest blank and add them later in each service's **Environment**
   tab.

## Required manual steps after the first deploy

These can't be automated by the blueprint — do them once, right after the
first deploy finishes:

1. **Match the DB password across services.** `render.yaml` generates one
   random `DB_PASSWORD` (shared by `checkout-web`/`checkout-worker` via the
   `checkout-shared` env var group) but MariaDB's own Docker image expects
   it under a different name (`MYSQL_PASSWORD`) that Render can't alias
   automatically. Copy the value:
   - Render dashboard → **Env Groups** → `checkout-shared` → copy the
     generated `DB_PASSWORD` value.
   - `checkout-mariadb` service → **Environment** → paste that same value
     into `MYSQL_PASSWORD`.
   - Set `MYSQL_ROOT_PASSWORD` on `checkout-mariadb` to anything (only used
     for direct `mysql -u root` access if you ever need it — doesn't need
     to match `DB_PASSWORD`).
   - Redeploy `checkout-mariadb` after setting these (env var changes on a
     private service require a manual restart/redeploy to take effect on
     first boot, since the MariaDB image only reads them once to bootstrap
     the database — see "Changing the DB password later" below if you ever
     need to change it after the DB already has data).
2. **Set admin login credentials** — `checkout-web` → **Environment** →
   `ADMIN_USERNAME` / `ADMIN_PASSWORD` (and `VIEWER_USERNAME`/
   `VIEWER_PASSWORD` if you want a read-only login too). Without these you
   can't log into the admin dashboard at all.
3. **Update `BASE_URL`** on `checkout-web` to whatever `.onrender.com` URL
   Render assigned it (shown at the top of the service page), or a custom
   domain if you attach one.
4. **Add whichever payment-processor credentials you actually need** for
   standby use — Shopify, Stripe, Helcim, Shippo, BTCPay, WPay, pymtz,
   NowPayments, Resend (email), etc. None of these are declared in
   `render.yaml` — every one already defaults to blank/disabled in
   `config.py`, exactly like an unconfigured `.env` on any other
   environment. The app runs fine with all of them off; each feature just
   stays disabled (same "not configured, skip" behavior used everywhere
   else in this codebase) until you add real values. Copy whichever ones
   you need from the VPS's `.env` (`ssh checkout-vps "grep KEY_NAME
   /srv/shared/checkout-server/.env"`) into the matching `checkout-web`
   environment variable. **`checkout-worker` needs the same webhook/API
   credentials as `checkout-web`** for anything its background tasks touch
   (currently just `expire-old-orders`, which doesn't need any external
   creds — safe to leave `checkout-worker` minimal for now).

## Verifying it worked

```
curl https://<your-service>.onrender.com/health
# {"status":"ok","environment":"production"}
```

Then log into `/peps-admin-2026/login` with the `ADMIN_USERNAME`/
`ADMIN_PASSWORD` you set. The dashboard will be empty (0 orders) — that's
expected on a fresh database, not a bug.

## What this actually gives you

- **If only the VPS's app/SSH is broken but the box itself is reachable**:
  this doesn't help much — the real fix is getting back into the VPS.
- **If the whole VPS goes down** (network, host, hardware): this Render
  deployment can take over serving checkout traffic (point DNS/Cloudflare
  at it, or share its URL directly), but it starts with **zero order
  history** — anything that happened on the VPS before the outage isn't
  here. Customers can place new orders; you won't see old ones in this
  admin panel.

## Keeping data in sync (optional, not set up yet)

If you want Render to have a reasonably current copy of real order data
(not just an empty DB), the straightforward option is a periodic
`mysqldump` from the VPS piped into Render's MariaDB — e.g. a cron job on
the VPS that runs nightly:

```bash
mysqldump -u checkout_user -p'<password>' --single-transaction checkout_db \
  | mysql -h <checkout-mariadb internal host> -u checkout_user -p'<DB_PASSWORD>' checkout_db
```

`checkout-mariadb` is a **private** service (no public IP) — this only
works run from something else already on Render's private network, or via
Render's SSH/port-forwarding tools for private services. This isn't set up
in `render.yaml` — ask if you want this built out.

## Redeploying after code changes

Render auto-deploys `checkout-web` and `checkout-worker` on every push to
the connected GitHub branch by default (`autoDeployTrigger` isn't set in
`render.yaml`, so it uses Render's default — deploy on every commit).
`checkout-mariadb` never needs redeploying for app code changes — it's just
running the stock `mariadb:10.11` image.

## Running migrations against Render's database

Table creation happens automatically on `checkout-web` startup
(`Base.metadata.create_all` in `main.py`) — a **fresh** database gets the
full current schema in one shot, so none of the old incremental scripts
under `migrations/` need to run against a brand-new Render database. You'd
only need to run one if a future code change adds a migration AFTER this
deployment already has data in it — same idea as running one on staging or
the VPS, just pointed at Render's DB host/credentials instead.
