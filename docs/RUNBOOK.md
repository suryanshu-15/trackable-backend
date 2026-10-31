# Trackable — Operations Runbook

For the people who deploy and run Trackable. It covers deployment, scheduled jobs,
backups and restores, monitoring, and what to do when something goes wrong.

---

## 1. Components

| Component | Purpose | Notes |
|---|---|---|
| PostgreSQL 16 | System of record | Only Django connects to it. Use a least-privilege role that owns the schema and nothing else. |
| Next.js frontend (`frontend/`, `node server.js`) | Screens, and the `/bff` proxy that holds sessions in httpOnly cookies | Stateless. The only component the browser talks to. |
| Django API (gunicorn) | All business rules, permissions, stock transactions | Stateless; scale by adding workers / containers. Reached by the frontend only — no need to expose it publicly. |
| Redis (optional) | Shared cache for login throttling | Needed when more than one worker / container runs. |
| Media volume | Uploaded invoices, lab reports | Never served by URL; downloads go through permission-checked API endpoints. |
| Reverse proxy (nginx, a load balancer, …) | TLS termination in front of the frontend; sets `X-Forwarded-Proto` and appends `X-Forwarded-For` | The frontend passes both to Django, which redirects to HTTPS and sends HSTS when `DEBUG=False`. Optionally route `/admin/` (Django admin, master data) to the backend. |

## 2. Configuration

All settings come from environment variables. `backend/.env.example` lists them.

**Required in production:**
- `DEBUG=False`
- `SECRET_KEY`: 32+ random characters. The app refuses to start otherwise.
- `ALLOWED_HOSTS`
- `CORS_ALLOWED_ORIGINS`
- `DB_*`
- `MEDIA_ROOT` on persistent storage
- `BUSINESS_TIMEZONE`
- `FRONTEND_BASE_URL`: QR labels encode `<FRONTEND_BASE_URL>/scan/<token>`, so keep it stable once labels are printed.

**Frontend (`frontend/.env.example`):**
- `BACKEND_URL`: where the frontend server reaches Django (e.g. `http://backend:8000`).
- `COOKIE_SECURE=true`: required in production (HTTPS). Set `false` only for local http testing with `npm start`; otherwise the session cookie is never sent back and every page bounces to login.
- `NEXT_PUBLIC_BUSINESS_TIMEZONE`: must equal Django's `BUSINESS_TIMEZONE`. It is baked in at build time.
- Django's `ALLOWED_HOSTS` must include the host name the frontend uses to reach it (`backend` in Compose).

**Recommended:**
- `REDIS_URL`
- `LOG_FORMAT=json`
- `FORWARDED_ALLOW_IPS=<proxy IP>`
- `GUNICORN_WORKERS`

**Business rules:** runtime rules live in the `SystemConfiguration` table (Django admin → *System configuration*). The documented defaults are in `apps/core/config.py`, and each key is explained in [DECISIONS.md](DECISIONS.md). Changes take effect immediately and need no deploy.

## 3. Deploy

### With Docker Compose

```bash
export SECRET_KEY=... DB_PASSWORD=... ALLOWED_HOSTS=wms.example.com \
       CORS_ALLOWED_ORIGINS=https://wms.example.com FRONTEND_BASE_URL=https://wms.example.com
docker compose up -d --build            # the entrypoint waits for the DB and runs migrations
docker compose exec backend python manage.py createsuperuser
```

### Without Docker

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --noinput
DEBUG=False .venv/bin/python manage.py collectstatic --noinput
.venv/bin/gunicorn config.wsgi:application -c gunicorn.conf.py     # behind the reverse proxy

cd ../frontend && npm ci && npm run build
cp -r .next/static .next/standalone/.next/ && cp -r public .next/standalone/
BACKEND_URL=http://127.0.0.1:8000 PORT=3000 node .next/standalone/server.js   # behind the reverse proxy
```

### Label printers and scanners

- **Printer:** any small 4-inch (104 mm print width) label printer that installs as a normal system printer works, because labels print from the browser in black only. Direct-thermal models need no ribbon and cost least; their print fades with heat and sunlight over months, so choose a thermal-transfer model if bags stay in stock longer or sit in hot areas.
- **Labels:** pre-glued 100 × 60 mm sticker rolls (white for package labels, green for release tags).
- In the printer driver set paper to **100 × 60 mm**, margins none, scale 100 %, and in the browser's print dialog turn headers / footers off. Print a test label from any package page and check that a USB scanner reads the barcode and a phone reads the QR code before go-live.
- USB barcode scanners must be in keyboard mode and send **Enter** after the code (the factory default for most models).
- Camera scanning on phones / tablets only works over HTTPS.
- QR codes encode `FRONTEND_BASE_URL/scan/<token>`. Keep that URL stable once labels are printed.

### Release checklist

1. CI is green: tests, `makemigrations --check`, a schema with no warnings, and the backup/restore smoke test.
2. **Take a backup** (`scripts/backup.sh`) immediately before migrating.
3. Run migrations on **staging** first, using a recent copy of production restored with `scripts/restore.sh`.
4. Deploy, then check `GET /health/` returns `{"status": "ok", "database": true}` and that the frontend login page loads and a demo / test user can sign in.
5. Run `manage.py reconcile_inventory` and confirm it exits 0.

Never use `seed_demo` in production. It refuses when `DEBUG=False` unless explicitly forced.

## 4. Scheduled jobs (cron)

| Schedule | Command | Purpose |
|---|---|---|
| Hourly | `manage.py run_alerts` | Alerts for: near-expiry / retest due, overdue samples, overdue put-away, low stock, ledger mismatch. Each alert goes once per day per recipient. If the job itself fails, admins get a notification. |
| Nightly | `manage.py reconcile_inventory` | Checks package balances and locations against the movement ledger. A non-zero exit means a mismatch, so alert on it. |
| Nightly | `scripts/backup.sh` | Database and media backup with checksums. Retention is `RETENTION_DAYS` (default 14). |
| Weekly | `scripts/backup_restore_check.sh` | Restores the latest state into a scratch database and compares every table's row count. |
| Weekly | `manage.py flushexpiredtokens` | Removes expired JWT refresh tokens from the blacklist tables. |
| Monthly | `manage.py apply_retention --apply` | Deletes audit events and receipt documents older than 1 year and finished lab samples older than 3 years (agreed retention). Without `--apply` it only reports. The purge itself is audited. |

Example crontab (as the app user, in `backend/`):

```cron
0 * * * *   cd /srv/trackable/backend && .venv/bin/python manage.py run_alerts
30 1 * * *  cd /srv/trackable/backend && .venv/bin/python manage.py reconcile_inventory || logger -p user.err "trackable: ledger mismatch"
0 2 * * *   cd /srv/trackable && BACKUP_DIR=/backups scripts/backup.sh
0 4 * * 0   cd /srv/trackable && scripts/backup_restore_check.sh
0 3 1 * *   cd /srv/trackable/backend && .venv/bin/python manage.py apply_retention --apply
```

## 5. Backups and restore

- `scripts/backup.sh` writes three things to `BACKUP_DIR`:
  - `trackable-db-<stamp>.dump`: a PostgreSQL custom-format dump, verified readable after writing.
  - `trackable-media-<stamp>.tar.gz`
  - `trackable-<stamp>.sha256`
- **Copy backups off the host** (object storage or another server). A backup on the same disk is not a backup.
- To **restore to a scratch database** for an investigation:
  ```bash
  TARGET_DB=trackable_inspect scripts/restore.sh backups/trackable-db-<stamp>.dump
  ```
- To **restore production**:
  1. Stop the API.
  2. Run `CONFIRM=yes scripts/restore.sh <db.dump> <media.tar.gz>`. The script refuses without `CONFIRM=yes`.
  3. Run `manage.py migrate`.
  4. Run `manage.py reconcile_inventory`.
  5. Start the API.
- **Restore tested:** `scripts/backup_restore_check.sh` runs in CI, and was run on 2026-10-29 against the development database: 52 tables, all row counts identical, migrations current, inventory reconciled.

## 6. Monitoring

- **Health:** `GET /health/` returns 200 when the database is reachable and 503 otherwise. Point the load balancer at it.
- **Logs:**
  - With `LOG_FORMAT=json`, every line includes a `request_id`. The same ID is returned in the `X-Request-ID` response header and stored on audit events as `correlation_id`, so a user-reported error can be traced end to end.
  - Credentials and tokens are never logged.
- **Ledger:** watch the nightly `reconcile_inventory` exit code, and the *ledger mismatches* figure on the management dashboard.
- **Business exceptions:** the management dashboard (`/api/v1/reports/management-dashboard/`) shows FIFO overrides, pending adjustments, open receipt discrepancies, active holds and active recalls.
- **Error tracking:** Sentry or similar can be added through a standard Django integration. It isn't bundled.

## 7. Incident playbooks

### Stock does not reconcile (`reconcile_inventory` fails)

1. Run `GET /api/v1/inventory/stock/reconciliation/` to list the affected packages and the problem with each.
2. Look at each package's history: `GET /api/v1/inventory/packages/<id>/timeline/` and `…/movements/`.
3. Find the cause. Never "fix" the database directly. Correct physical counts through a **stock adjustment**: a CORRECTION carrying the counted quantity, requested by the warehouse and approved by a manager. It appears in the ledger and the audit trail.
4. If the database was edited directly, restore the affected records from a backup into a scratch database to compare.

### Material was released by mistake

1. Place a **hold** immediately with `POST /api/v1/laboratory/holds/` (per package, or by material and lot). This blocks issue at once without changing the QC record.
2. Check `GET /laboratory/holds/<id>/impact/` for what has already gone to which factory.
3. The lab records a corrective disposition: RETEST, or REJECTED with a reason. History is kept.
4. If material already reached production, escalate to a **recall** (`hold_type=RECALL`). It also covers packages that were already issued, for traceability.

### A user left or their credentials were compromised

1. **Deactivate the user** (`POST /api/v1/auth/users/<id>/deactivate/`). Their refresh tokens stop working at the next refresh, and the access token expires within `JWT_ACCESS_TOKEN_LIFETIME_MINUTES`.
2. Review their recent actions: `GET /api/v1/audit-events/?actor=<id>`.

### Login is being brute-forced

Login is throttled per client IP (`LOGIN_THROTTLE_RATE`, default 10/min) and over-limit attempts get 429. Behind a proxy, make sure `FORWARDED_ALLOW_IPS` is set so the real client IP is used, and set `REDIS_URL` so the limit is shared across workers.

### Need to undo a business record

Audit records, movements and label events are immutable by design. Use the compensating workflow instead:

| To correct | Use |
|---|---|
| Wrong weights at the gate | `correct-weights` |
| Wrong quantities in stock | Stock adjustment |
| Wrong issue | Return |
| Wrong quality decision | New disposition, or a hold |

## 8. Data retention

Agreed periods: **audit events and documents 1 year, lab records 3 years** (settings `retention.*_days`).
`manage.py apply_retention` reports what is past retention; `--apply` deletes it (files after the
database commit) and records one `DELETE Retention` audit event with the counts. Before the first
real run, restore a recent backup into a scratch database and run it there. Backups older than the
retention period should be rotated out too (`RETENTION_DAYS` in `scripts/backup.sh`).
