# Trackable — Warehouse Material Traceability System

> Project documents (`docs/` — decisions, runbook, functionality checklist, test guide — and the
> requirements specification) are kept outside this repository; links to them below work only in a
> local copy that has them.

## Quick Start (Development)

### Prerequisites
- Python 3.12+
- Node.js 20+
- PostgreSQL 16 (database: `trackable`, owner: `trackable`)

One-time database setup (the role needs LOGIN, and CREATEDB so tests can
create their throwaway database):

```bash
sudo -u postgres psql -c "ALTER ROLE trackable WITH LOGIN CREATEDB PASSWORD 'trackable';"
# or, if the role/database do not exist yet:
sudo -u postgres psql -c "CREATE ROLE trackable WITH LOGIN CREATEDB PASSWORD 'trackable';"
sudo -u postgres createdb -O trackable trackable
```

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit values
python manage.py migrate
python manage.py seed_demo --with-sample-flow   # DEMO master data + receipts at each stage (refuses when DEBUG=False)
python manage.py runserver
python manage.py test         # needs CREATEDB on the DB role
python manage.py reconcile_inventory   # ledger check; exits 1 on mismatch (cron/monitoring)
python manage.py run_alerts            # expiry / overdue / low-stock / ledger alerts (cron, hourly)
../scripts/backup_restore_check.sh     # backup → restore into scratch DB → compare every table
python manage.py apply_retention       # retention report (1 y audit / documents, 3 y lab); --apply to purge
```

- API docs: http://localhost:8000/api/v1/schema/swagger-ui/
- Health check: http://localhost:8000/health/
- Demo logins: `demo_<role>` (e.g. `demo_gate`, `demo_admin`) with `DEMO_USER_PASSWORD`.

### Receiving flow (API)

1. **Gate** `POST /receiving/goods-receipts/` → add lines `POST /receiving/invoice-lines/` → `…/register/` → `…/submit-unloading/`
2. **Receiving** `…/start-unloading/` → per line `POST /receiving/invoice-lines/{id}/verify/`
   (discrepancies → receipt `EXCEPTION`; manager `POST /receiving/verifications/{id}/accept-discrepancy/`)
3. `POST /receiving/invoice-lines/{id}/packages/` (send an `Idempotency-Key` header) → `POST /inventory/packages/{id}/print-label/`
4. `…/verification-pending/` → `…/mark-unloaded/`
5. **Warehouse** scan `GET /inventory/packages/resolve/?code=…` → `POST /inventory/packages/{id}/putaway/`
   (pending-QC stock → quarantine location); receipt becomes `STORED` automatically
6. Stock: `/inventory/stock/{dashboard,balances,ageing,expiry,low-stock,reconciliation}/`,
   package history `/inventory/packages/{id}/timeline/`
7. **Lab** `POST /laboratory/samples/` (packages of one lot) → `…/collect/` → `…/start-testing/` →
   `POST /laboratory/test-runs/{id}/results/` → `…/submit/` →
   **Reviewer** `POST /laboratory/samples/{id}/disposition/` (RELEASED / REJECTED / ON_HOLD / RETEST / PARTIAL)
8. Released stock: green tag `POST /inventory/packages/{id}/print-release-tag/`. Holds / recalls:
   `/laboratory/holds/` (+ `…/impact/` for traceability)
9. **Factory** `POST /inventory/requests/` → **Warehouse** `…/{id}/suggestions/` (FIFO/FEFO pick list),
   optional `…/{id}/allocate/` (reserve) → `POST /inventory/issues/` (`Idempotency-Key` header;
   out-of-order picks → 409, a manager can override with `override_reason`) → **Factory** `…/issues/{id}/acknowledge/`
10. Stage 9: `POST /inventory/returns/` (returned qty → new blocked child package), `/inventory/disposals/`
    (scrap / supplier return), `/inventory/adjustments/` (+ `…/approve/`, `…/reject/` by a manager)
11. Reports: `GET /reports/` (list for your role) → `GET /reports/{key}/?…filters` (`&export=csv|xlsx`),
    `GET /reports/management-dashboard/`, global search `GET /search/?q=…`, audit trail `GET /audit-events/`

### Frontend

The frontend is a separate repository, [trackable](https://github.com/suryanshu-15/trackable).
Clone it into this repository's `frontend/` folder (docker-compose and `npm run gen:api` expect that):

```bash
git clone https://github.com/suryanshu-15/trackable.git frontend
cd frontend
npm install
cp .env.example .env.local    # BACKEND_URL=http://localhost:8000
npm run dev                   # http://localhost:3000 (the backend must be running)
npm test                      # 30 tests: forms, scan workflow, permissions, error states, API proxy
npm run typecheck && npm run lint
npm run gen:api               # after backend serializer changes: regenerate typed API contract
```

`npm run gen:api` reads `backend/schema.yml`; refresh it first with
`cd backend && python manage.py spectacular --file schema.yml`. Backend CI fails if `schema.yml` is stale.

### Trying it out

Start the backend (`runserver`) and the frontend (`npm run dev`), open http://localhost:3000 and
sign in as any demo user (`demo_<role>` / `demo-pass-123!`). Each role lands on its own dashboard
and sees only its menu. With `--with-sample-flow` the database contains:

| Receipt | State | Try (user) |
|---|---|---|
| `DEMO-INV-A` | 1 t in 4 bags of 0.25 t, each sampled and **released**, on rack A-R1; request `DEMO-PO-1001` (0.75 t) allocated, one bag issued | `demo_warehouse`: Material requests → issue the remaining 0.5 t (two whole bags; scan a different bag to see the FIFO block); `demo_factory`: acknowledge it; `demo_lab_reviewer`: release tag, hold / recall |
| `DEMO-INV-B` | 2 bags in quarantine, one sample per bag **in testing** | `demo_lab_tech`: Laboratory → sample → enter results → submit; `demo_lab_reviewer`: record decision (the technician cannot) |
| `DEMO-INV-C` | 2.4 t **awaiting unloading** | `demo_receiving`: Unloading → truck → start unloading, verify (accept the whole line or reject it), create bags, print labels, put away by scanning |
| `DEMO-INV-D` | Gate **draft**, no lines | `demo_gate`: add invoice lines (in t, with expiry and retest dates), register, send for unloading |

Business rules in force (see [docs/DECISIONS.md](docs/DECISIONS.md)): bags counted in tonnes, whole-line
acceptance, one sample per bag, whole-bag issue against a material request, expiry extension after retest.

`demo_manager` / `demo_auditor`: Management dashboard, Usage analysis, Reports (CSV / Excel), Audit trail.
Swagger stays available at http://localhost:8000/api/v1/schema/swagger-ui/.
A checklist of every function, who uses it and where: [docs/FUNCTIONALITY.md](docs/FUNCTIONALITY.md).
A step-by-step test with demo values and every input field explained: [docs/END_TO_END_TEST_GUIDE.md](docs/END_TO_END_TEST_GUIDE.md).

**Scanning:** a USB barcode scanner works in any scan box (it types the code and presses Enter).
The camera button needs HTTPS or `localhost`. **Labels** print at 100 × 60 mm, one per page —
set the label printer's paper size to match and margins to none.

---

## Environment Variables

See `backend/.env.example` and `frontend/.env.example`.
**Never commit real secrets.** `.env` is git-ignored.

Design defaults and the open business questions are in
[docs/DECISIONS.md](docs/DECISIONS.md).

---

## Roles

| Role | Key |
|---|---|
| Administrator | ADMIN |
| Gate Operator | GATE |
| Receiving/Unloading Worker | RECEIVING |
| Warehouse Operator | WAREHOUSE |
| Lab Technician | LAB_TECH |
| Lab Reviewer / QC Manager | LAB_REVIEWER |
| Factory / Requester | FACTORY |
| Auditor (read-only) | AUDITOR |
| Manager | MANAGER |

---

## Architecture

```
Browser ──HTTPS──▶ Next.js (pages + /bff proxy) ──▶ Django (DRF) ──▶ PostgreSQL
                   JWTs in httpOnly cookies             │
                                                        └──▶ Media files (MEDIA_ROOT)
```

All business rules, permission checks, state transitions, and stock
transactions are enforced in the Django service layer. The browser only talks to
Next.js; its `/bff/*` route attaches the session token and forwards to `/api/v1/*`, so
tokens are never readable by page scripts. Screens show the actions the API says are
allowed (`allowed_actions`, `allowed_transitions`) and re-read server state after every
change — nothing about inventory is kept in browser storage.

---

## Phase Status

| Phase | Backend | Frontend |
|---|---|---|
| 1 — Foundation (Django, DRF, JWT, roles, master data, audit, CI) | ✅ done, tested | ✅ login, role navigation, user admin |
| 2 — Gate & Receipt | ✅ done, tested | ✅ gate dashboard, receipt form (duplicate warning), documents, timeline |
| 3 — Unloading & Labels | ✅ done, tested | ✅ unloading queue, verification, packages, label printing, scan-to-open |
| 4 — Warehouse & Inventory | ✅ done, tested | ✅ put-away, transfer, split, stock & availability dashboards |
| 5 — Laboratory & Release | ✅ done, tested | ✅ lab dashboard, sample workflow, results, decisions, holds / recalls |
| 6 — Issue & FIFO/FEFO | ✅ done, tested | ✅ requests, pick list, issue with override, acknowledge, returns, disposals, adjustments |
| 7 — Reports, Hardening, Deployment | ✅ done, tested | ✅ management dashboard, reports & exports, audit, search, notifications |

Verified on the dev machine: 261 backend tests, 30 frontend tests, a 47-check end-to-end run
through the browser-facing proxy covering every role (on a scratch database), and screenshots
at desktop, tablet and phone widths. Not verified here: the Docker images (no Docker installed).

Operations (deploy, cron jobs, backup/restore, monitoring, incident playbooks): [docs/RUNBOOK.md](docs/RUNBOOK.md).
