# Staging environment

A full, isolated copy of checkout-server for testing changes before they
reach production. Nothing here touches real customers, real payments, or
production data — that's the entire point.

**URL**: https://staging.pepscheckoutportal.com
**Admin login**: https://staging.pepscheckoutportal.com/peps-admin-2026/login
(credentials are in staging's `.env` under `ADMIN_USERNAME`/`ADMIN_PASSWORD` —
not repeated here since this file is committed to git)

---

## Why this exists

Before staging existed, every change — including code that touches
payments — went straight from a local edit to the production VPS. This
environment exists so that stops being true. **Nothing should reach
production without having run on staging first.**

## Architecture

Staging is a second, fully independent instance of the app running
alongside production on the same VPS (`checkout-vps`, port 2223 for SSH).
Nothing is shared except the physical machine.

| | Production | Staging |
|---|---|---|
| Directory | `/srv/shared/checkout-server` | `/srv/shared/checkout-server-staging` |
| Web app port | `8000` | `8001` |
| Domain | `pepscheckoutportal.com` (+ aliases) | `staging.pepscheckoutportal.com` |
| Database | `checkout_db` | `checkout_staging` |
| DB user | `checkout_user` | `checkout_staging_user` |
| Redis | index `0` | index `1` |
| Web systemd unit | `checkout.service` | `checkout-staging.service` |
| Celery worker unit | `checkout-worker.service` | `checkout-staging-celery.service` |
| Celery beat unit | `checkout-beat.service` | *(none — staging has no scheduled jobs beyond what the worker handles)* |
| nginx config | `/etc/nginx/sites-available/pepscheckoutportal` | `/etc/nginx/sites-available/staging-pepscheckoutportal` |

Both domains sit behind Cloudflare, which terminates the real public TLS
cert. The origin nginx just needs *some* cert to speak HTTPS to Cloudflare
— it reuses the same self-signed one production already uses
(`/etc/ssl/certs/pepscheckout.crt`), since Cloudflare's "Full" SSL mode
(not "Full Strict") doesn't validate the origin cert's hostname.

## Credentials

All real secrets (DB passwords, admin login, Shippo/Shopify tokens, etc.)
live in `/srv/shared/checkout-server-staging/.env` on the VPS — **never in
this file or anywhere in git** (`.env` is gitignored). If you need a value,
SSH in and read it directly:

```
ssh -p 2223 checkout-vps
cat /srv/shared/checkout-server-staging/.env
```

If you ever need MySQL **root** access and don't have the password handy,
Debian/Ubuntu MariaDB installs keep a maintenance account for exactly this:

```
sudo cat /etc/mysql/debian.cnf
```

That file has a `[client]` block with `user=root` and a working password —
use it with `mysql -u root -p'<that password>'`.

## The deploy workflow

Git is the source of truth. A bare repo lives at
`/srv/shared/checkout-server.git` on the VPS; the local dev machine, prod,
and staging are all clones of it.

```
# 1. Edit + commit locally, push to the shared bare repo
git push vps main

# 2. Pull into staging and restart
ssh -p 2223 checkout-vps
cd /srv/shared/checkout-server-staging
git pull
sudo systemctl restart checkout-staging.service checkout-staging-celery.service

# 3. Actually test it — click through the relevant flow on
#    https://staging.pepscheckoutportal.com, check the admin dashboard, etc.

# 4. Only once you're satisfied, promote the same commit to production
cd /srv/shared/checkout-server
git pull
sudo systemctl restart checkout.service
```

If a change adds/modifies database columns, run the migration on staging
**before** restarting staging's service, and on prod before restarting
prod's — migrations are idempotent (safe to run more than once), so when
in doubt, just run it:

```
cd /srv/shared/checkout-server-staging   # or checkout-server for prod
venv/bin/python -m migrations.<name>
```

**Claude cannot run `sudo` itself** (no interactive TTY on this box) — any
step above that needs `sudo` has to be run by a human. Claude can do
everything else (git operations, migrations, `venv/bin/python` compile
checks, `curl` health checks) directly over SSH.

## Common tasks

**Check both services are healthy:**
```
curl -s https://staging.pepscheckoutportal.com/health
# {"status":"ok","environment":"staging"}

systemctl is-active checkout-staging.service checkout-staging-celery.service
```

**Tail logs:**
```
journalctl -u checkout-staging.service -f
journalctl -u checkout-staging-celery.service -f
```

**Reset staging's database to empty** (e.g. after a lot of test orders pile
up) — staging's schema is fully reproducible from migrations, so this is
safe:
```
mysql -u checkout_staging_user -p'<password from .env>' -e "DROP DATABASE checkout_staging; CREATE DATABASE checkout_staging;"
sudo systemctl restart checkout-staging.service   # re-creates all tables on startup
```

**Copy one brand's config from production** (so a test checkout page shows
real branding instead of generic defaults) — brand rows have no secrets in
them, just logo/color/discount config, so this is safe to copy directly:
```
mysqldump -u checkout_user -p'<prod password>' checkout_db brands --where="domain='example.com'" | \
  mysql -u checkout_staging_user -p'<staging password>' checkout_staging
```

## Known gotchas

- **Celery + systemd**: if a `celery worker`/`celery beat` process crashes
  and gets auto-restarted by `Restart=always`, its already-forked
  prefork child processes can survive the crash and keep running
  alongside the new restart, leaving duplicate orphaned processes. Both
  Celery units here have `KillMode=control-group` set specifically to
  prevent this (kills the whole cgroup on stop/restart, not just the
  tracked PID). If you ever see more Celery processes running than
  expected (`ps aux | grep celery`), check this setting is still in the
  unit file before assuming something else is wrong.
- **`celerybeat-schedule`** (in the prod/staging working directory) is
  Celery Beat's persisted schedule state — safe to delete if Beat is
  stuck erroring about a corrupted/locked schedule file; it gets
  recreated fresh on the next start.
- Production's `checkout-worker.service`/`checkout-beat.service` predate
  this staging setup (created 2026-07-29) — don't confuse them with the
  staging-specific `checkout-staging-celery.service`.

## What's deliberately different from production (safety guardrails)

- Every payment processor is **disabled by default** in staging's `.env`.
  Stripe Direct is the one safe to fully enable with test-mode keys
  (`sk_test_...`) if you need to test a live payment flow — its test mode
  is a real sandbox, not a simulation.
- Interac/Zelle "send money to" addresses are fake placeholders
  (`staging-donotuse@example.com`) so a test checkout can never show a
  real address to send real money to.
- `SHIPPO_API_TOKEN` is a **live** key (Shippo has no meaningful sandbox
  distinction for this account) — rate lookups are free, but buying a
  label is real money and creates a real trackable shipment. Never
  purchase a test label without explicit sign-off first.
