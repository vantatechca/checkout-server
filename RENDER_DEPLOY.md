# Render standby deployment

A second, independent copy of checkout-server on Render — usable if the VPS
goes down. This is **not** a live mirror of the VPS: it runs its own
Postgres database, starting empty. Read "What this actually gives you"
below before relying on it during a real outage.

## Architecture

| | VPS (primary) | Render (standby) |
|---|---|---|
| App | systemd, `checkout.service` | Render Web Service (`checkout-web`) |
| Background tasks | systemd, `checkout-worker.service` | Render Background Worker (`checkout-worker`) |
| Database | MariaDB, local to the VPS | Postgres, Render's own managed database (`checkout-db`) |
| Redis | local to the VPS | Render Key Value (`checkout-redis`) |
| Data | real production orders | **separate, starts empty** |

The two databases are **not synced or replicated** — this is deliberately
the simplest version that works, not a hot-standby with live data. See
"Keeping data in sync" below for what to do about that.

**Why Postgres, not MariaDB, here:** Render has no managed MySQL/MariaDB
product, only Postgres. Rather than run MariaDB ourselves as a private
service (more moving parts — custom Docker image, disk management, manual
password syncing), the app now supports both dialects: a new `DB_DIALECT`
setting in `config.py` (`"mysql"` by default — the VPS and staging need
**zero `.env` changes**, they just keep working) or `"postgres"` (what
`render.yaml` sets for this deployment). Every column type this app's
models use is dialect-agnostic SQLAlchemy, so no schema changes were
needed — see the `Adapt app to Postgres` commit for the small set of
changes this took (`config.py`'s `DATABASE_URL`, a one-line dialect guard
in `database.py`, and two extra drivers in `requirements.txt`).

## One-time setup

1. This repo is already on GitHub. Push the branch with these changes if
   you haven't already.
2. In the Render dashboard: **New +** → **Blueprint** → connect the repo
   (pick the right branch if prompted). Render finds `render.yaml` at the
   repo root automatically and shows you the services it's about to
   create (`checkout-db`, `checkout-redis`, `checkout-web`,
   `checkout-worker`). Click **Apply**.
3. Render will prompt for every `sync: false` variable in `render.yaml`
   during this flow — fill in what you have (see below); leave the rest
   blank and add them later in each service's **Environment** tab.

## Required manual steps after the first deploy

1. **Set admin login credentials** — `checkout-web` → **Environment** →
   `ADMIN_USERNAME` / `ADMIN_PASSWORD` (and `VIEWER_USERNAME`/
   `VIEWER_PASSWORD` if you want a read-only login too). Without these you
   can't log into the admin dashboard at all.
2. **Update `BASE_URL`** on `checkout-web` to whatever `.onrender.com` URL
   Render assigned it (shown at the top of the service page), or a custom
   domain if you attach one.
3. **Add whichever payment-processor credentials you actually need** for
   standby use — Shopify, Stripe, Helcim, Shippo, BTCPay, WPay, pymtz,
   NowPayments, Resend (email), etc. None of these are declared in
   `render.yaml` — every one already defaults to blank/disabled in
   `config.py`, exactly like an unconfigured `.env` on any other
   environment. The app runs fine with all of them off; each feature just
   stays disabled (same "not configured, skip" behavior used everywhere
   else in this codebase) until you add real values. Copy whichever ones
   you need from the VPS's `.env` into the matching `checkout-web`
   environment variable. `checkout-worker` currently doesn't need any of
   these — its only scheduled task (`expire-old-orders`) has no external
   dependencies.

That's it — no database-password-matching step is needed here (unlike an
earlier draft of this file that ran MariaDB ourselves); Render's
`fromDatabase` references wire the real Postgres credentials into both
`checkout-web` and `checkout-worker` automatically.

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
(not just an empty DB), you'd need a periodic export from the VPS's
MariaDB into Render's Postgres — this isn't a simple `mysqldump | mysql`
pipe anymore since the two are different database engines now (a tool like
`pgloader`, or a small script that reads via SQLAlchemy from one and
writes to the other, would do it). Not set up in `render.yaml` — ask if
you want this built out.

## Redeploying after code changes

Render auto-deploys `checkout-web` and `checkout-worker` on every push to
the connected GitHub branch by default. `checkout-db` and `checkout-redis`
never need redeploying for app code changes.

## Running migrations against Render's database

Table creation happens automatically on `checkout-web` startup
(`Base.metadata.create_all` in `main.py`, dialect-agnostic) — a **fresh**
database gets the full current schema in one shot, so none of the old
incremental scripts under `migrations/` need to run against a brand-new
Render database (those scripts also use MySQL-specific raw SQL —
`information_schema` column checks, `TINYINT(1)` — and would need
Postgres-equivalent versions if a future migration is ever needed against
a Render database that already has data in it).
