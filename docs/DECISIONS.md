# Design decisions & open questions

Defaults chosen where the requirements leave a choice (spec §17.3). Items marked
**OPEN** change inventory, quality or compliance behaviour and need business sign-off.

## Decided (safe defaults, change if needed)

Where the business answers in *Confirmed by the business* below differ from a row here, the answers apply.

| Topic | Decision |
|---|---|
| Authentication | JWT bearer tokens (SimpleJWT), refresh rotation + blacklist. No cookie session for the API, so CSRF does not apply to API calls. |
| Roles | One primary role per user (`users.User.role`); superusers bypass role checks. Enforced server-side on every endpoint. |
| Timezone | Stored in UTC; `TIME_ZONE = INDIAN-TIMEZONE` so "today", date filters and reference numbers follow the warehouse day. |
| Duplicate invoice | Key = supplier + invoice number (case-insensitive) + invoice date, configurable via `SystemConfiguration` key `receipt.duplicate_check_fields` (JSON list). A match returns **409 `duplicate_invoice`**; resubmitting with `acknowledge_duplicate=true` creates the receipt and audits the acknowledgement. Cancelled receipts never count. |
| Weights | Net = gross − tare, always derived server-side (read-only). Gross ≥ tare enforced in service **and** DB constraint. Weights require a unit. |
| Truck weight vs lines | Truck weight is used **only for reconciliation**: compared with the sum of declared line weights in the same unit; tolerance `receipt.weight_tolerance_percent` (default 0.5 %). Never used to infer product quantity. |
| Editing submitted gate data | Header is editable only in `DRAFT`. After that, weights change only through `POST …/correct-weights/` (ADMIN, GATE) with a mandatory reason; before/after values are audited. |
| Invoice lines | Editable in `DRAFT` and `GATE_REGISTERED`; locked from `AWAITING_UNLOADING`. Submitting for unloading needs ≥ 1 line. Line numbers auto-assign. |
| Deletion | Receipts and users cannot be deleted (cancel / deactivate). Master data referenced by transactions returns 409; deactivate instead. Invoice lines can be deleted only while editable and before packages/verification exist (deletion audited). |
| Documents | Validated by size, extension allowlist **and content signature**; stored under a random name. Media is **not** served by URL; downloads go through an authenticated, role-checked, audited endpoint. FACTORY role cannot read receipts or documents. |
| Package status model | Three independent dimensions (spec "critical design reminder"): **lifecycle** `status` (CREATED → AWAITING_PUTAWAY → STORED → ISSUED / REPACKED / DISPOSED / SUPPLIER_RETURNED), **quality** `qc_status`, and **blocking flags** `is_on_hold` / `is_recalled`. Physical location is `current_location`. "Stored" never implies "approved". |
| Availability | Never stored. Derived at query time by `Package.objects.available()` / `Package.blocking_reasons()`: STORED, QC PASSED or NOT_REQUIRED, not held, not recalled, quantity > 0, not expired (expiry > today), retest not overdue. So it cannot go stale when a date passes. |
| QC requirement | `Material.qc_required = False` is the explicit policy that gives packages `NOT_REQUIRED`; otherwise they start `PENDING_SAMPLING`. |
| Quarantine | A package whose QC is pending, failed or on hold may only be put away / transferred into a location flagged `is_quarantine`. |
| Storable locations | Put-away/transfer only into an active location with no active child locations (i.e. a leaf such as a shelf or bin). |
| Unloading verification | Worker records received / damaged / mismatched / accepted quantity and observations. Short/excess and the discrepancy type are computed server-side, using a configured `UoMConversion` when units differ; with no conversion the line is flagged `UNIT_MISMATCH`. Invoice-declared values are never modified. A discrepant line moves the receipt to `EXCEPTION` automatically. |
| Discrepancy acceptance | ADMIN or MANAGER, with a reason. Separation of duties: the person who recorded the discrepancy cannot accept it (`receiving.discrepancy_acceptance_separation`, default on). Re-recording a line voids a previous acceptance. |
| Unidentified material | If the invoice line has no master material, the worker records `identified_material` on the verification; packages use it, the invoice line is untouched. |
| Package creation | Only for lines that are VERIFIED or EXCEPTION_ACCEPTED, while the receipt is unloading; total packaged ≤ accepted quantity; package unit = received unit. Lot data defaults from the invoice line and can be overridden per package. Expiry/retest dates are mandatory when the material (or its category) requires them. Retry-safe via the `Idempotency-Key` header. |
| Completing unloading | `mark_verification_pending` needs every line verified or accepted; `mark_unloaded` additionally needs each line's accepted quantity fully packaged. Put-away is allowed once the receipt is UNLOADED; the receipt moves to PUTAWAY_IN_PROGRESS and STORED automatically. |
| Labels | QR encodes `<FRONTEND_BASE_URL>/scan/<opaque token>`; the printed 1-D barcode value is the package ID. Labels carry no mutable status. The first print moves CREATED → AWAITING_PUTAWAY (put-away requires a printed label); reprints keep the ID and need a reason. Scans resolve QR URL, token or package ID and are recorded in the timeline. |
| Repacking | Split moves quantity into new child packages (new IDs and labels, same lot, QC status, flags and FIFO date, `parent_package` link). A fully split parent becomes REPACKED. Merge is not supported. |
| Ledger | Every quantity change writes an immutable `InventoryMovement` with signed `quantity_delta` and `balance_after`; location changes are flagged. `GET /inventory/stock/reconciliation/` and `manage.py reconcile_inventory` (non-zero exit on mismatch) check balances and locations against it. |
| FIFO date | `Package.received_at` = truck arrival time, inherited by child packages. Ageing buckets use it (`stock.ageing_buckets`, default 30/60/90 days). |
| Returns (draft) | Returned quantity goes back to the same package with `qc_status` RETEST_REQUIRED (or ON_HOLD when no QC needed) — never directly available. |
| Stock count correction (draft) | `CORRECTION` adjustments carry the physically counted quantity; INCREASE/DECREASE carry a positive amount. Approver must differ from the requester. |
| QC gate | Quality changes (release, reject, hold, recall) happen only through laboratory workflows. A lab disposition can never cover packages outside its sample. |
| Sample scope | Business default `SINGLE`: one bag per sample (see confirmed answers). The engine also supports `LOT` (several packages of one material and lot; packages without a batch must share an invoice line) and `PACKAGE` (no lot restriction). The result applies only to those packages plus child packages later repacked from them — never to other packages of the lot. A package can be in only one active sample. |
| Sampling stock | Quantity drawn at collection leaves stock through the ledger (`SAMPLE` movement), so sampled quantity reconciles. Covered packages go PENDING_SAMPLING → SAMPLED → TESTING. |
| Default tests | The material's configured test requirements plus any tests the invoice line asked for; a sample cannot be created with no tests. |
| Result evaluation | Server-side, with limits snapshotted on the result: numeric min/max inclusive; boolean; text against acceptable values (case-insensitive). Text tests with no criterion need an explicit status + interpretation from the analyst. |
| Result corrections | Allowed only while the run is open; each correction increments `revision` and is audited with before/after. After submission results are locked. |
| Retest | A RETEST decision opens a new run; earlier runs and failed results are kept (`is_current = False`). |
| Disposition | LAB_REVIEWER / ADMIN only, reason required, only on submitted results. Separation of duties (`lab.separation_of_duties`, default on): nobody who entered or submitted the run's results may decide. RELEASED / PARTIAL are refused while a required result fails, is inconclusive or missing (failed optional tests block only if `lab.optional_failures_block_release` is on). ON_HOLD keeps the sample open for a final decision. PARTIAL releases a chosen subset; the rest becomes REJECTED or ON_HOLD. A RELEASED decision may set a new retest date (periodic retest). |
| Holds & recalls | LAB_REVIEWER / ADMIN, reason required; by package list or by material + lot. Holds set `is_on_hold` (quality status untouched), recalls set `is_recalled` and include already-issued packages for traceability (`…/impact/` lists remaining stock and factory issues). Release needs notes; a package stays flagged while another active hold of the same type covers it. |
| Green release tag | `print-release-tag` succeeds only when the server confirms the package is released and not blocked (QC, hold, recall, expiry, retest); reprints need a reason; every print is recorded. |
| Notifications | In-app: sample requested → lab technicians; results submitted → reviewers; reject / hold / retest / partial and holds / recalls → warehouse, managers, reviewers. |
| Material requests | Raised by FACTORY (or WAREHOUSE / ADMIN on their behalf), one line per material + unit. Factory users only see requests they raised and issues for those requests or that they received (object-level scoping, also in global search). |
| Reservations | Optional `allocate` reserves eligible stock in FIFO / FEFO order per request line; reserved quantity cannot be issued, split, disposed or adjusted away by anyone else. Re-allocation recomputes the target; release, cancel, close and fulfilment free reservations. |
| FIFO / FEFO | `issue.allocation_policy` AUTO (default): FEFO (earliest expiry or retest date) when the material / its category requires dates or any eligible package has one, otherwise FIFO by received date. Packages with equal keys (same lot / arrival) are interchangeable. A selection is compliant when every earlier package is taken in full; packages reserved for the request count as compliant. |
| FIFO override | Out-of-order picks return **409 `fifo_override_required`** with the skipped packages. Only `issue.override_roles` (default ADMIN, MANAGER) can override, with a reason; the issue is flagged, each line records `fifo_compliant`, an OVERRIDE audit event is written and managers / admins are notified. `issue.fifo_override_policy = FORBID` refuses all overrides. Blocked stock can never be overridden. |
| Issue | ADMIN / WAREHOUSE / MANAGER; one atomic, retry-safe (`Idempotency-Key`) transaction; row locks in primary-key order; QC status and lot snapshotted per line; who received it is mandatory; the factory acknowledges receipt. Whole-package-only materials: `Material.allow_partial_issue = False`. |
| Returns | Returned quantity becomes a **new child package** of the issued one (new ID and label, same lot and FIFO date), never mixed back into stock: RETEST_REQUIRED when QC applies, otherwise an automatic inspection hold for a reviewer to release. Cannot exceed issued − already returned. |
| Disposal / supplier return | Reason required; disposing of released, usable stock needs ADMIN / MANAGER; reserved quantity is protected; a package emptied this way becomes DISPOSED / SUPPLIER_RETURNED. |
| Stock adjustments | Requested by WAREHOUSE / ADMIN, approved or rejected (with notes) by ADMIN / MANAGER other than the requester; nothing changes until approval; cannot go below zero or below reserved quantity. |
| Reports & exports | 14 reports (spec §12) with role-based access; CSV streams any size, Excel capped at 100 000 rows; exports use the same filters and permissions as the JSON view and are audited (`DOWNLOAD Report`). |
| Alerts | `run_alerts` (cron): expiry / retest window, overdue samples, put-away overdue (`alerts.putaway_overdue_hours`, default 24), low stock, ledger mismatch; once per recipient per entity per day; a failing run notifies admins. |
| Hardening | Login throttled per IP (`LOGIN_THROTTLE_RATE`); request IDs in logs, responses and audit events; app refuses to start with `DEBUG=False` and a weak `SECRET_KEY`; HTTPS / HSTS / secure cookies outside debug; list endpoints tested for constant query counts. |
| Frontend sessions | The browser talks only to Next.js. Login stores the JWT pair in **httpOnly, SameSite=Lax** cookies (Secure when `COOKIE_SECURE`, default on in production); the `/bff/*` route attaches the bearer token, refreshes it once for concurrent requests (refresh tokens rotate) and forwards the client IP (the reverse proxy's rightmost `X-Forwarded-For` entry) and scheme. No token is ever readable by page scripts; no inventory state is kept in browser storage. |
| Frontend permissions | Navigation is shaped by role, and action buttons come from the server's `allowed_actions` / `allowed_transitions`; the API remains the only enforcement point. |
| Labels | 100 × 60 mm, one per page: QR (opens `/scan/<token>`), Code 128 barcode of the package ID for 1D USB scanners, material, lot, **packed** quantity, expiry / retest, supplier, receipt / line. A print is recorded only after the user confirms it came out; the first print releases the package for put-away; reprints need a reason. The green release tag is a separate, visibly green label. |
| Scanning | Every scan box accepts keyboard-wedge USB scanners (code + Enter). Camera scanning uses the browser's BarcodeDetector, falling back to ZXing; it needs HTTPS (or localhost). Locations can be scanned by code during put-away / transfer. |


## Confirmed by the business (2026-10-30)

| # | Question | Answer | How it is applied |
|---|---|---|---|
| 1 | Package type / quantity | Bags, counted in tonnes | `package.type = BAG` (printed on labels); `package.unit_codes = ["T"]`: lines are verified and bags created in tonnes, otherwise refused. Gate truck weights stay in kg. |
| 2, 5d | Sampling scope | One sample per bag; the result applies to that bag only | `lab.sample_scope = SINGLE`. Selecting several bags requests one sample each. Partial decisions are hidden (a sample has one bag). Bags split from a sampled bag follow its result. |
| 3 | Partial acceptance of an invoice line | No — accepted or rejected as a whole | `receiving.line_acceptance = WHOLE`: accept = everything received; reject = nothing accepted, reason required, flagged `REJECTED` and needs a manager's acceptance before the truck completes. |
| 4 | Expiry vs retest | Both on every bag | Materials require expiry **and** retest dates by default; bags cannot be created without them. Expired or retest-overdue bags are blocked from issue. |
| 5 | Weight corrections | Admin + gate | Unchanged. |
| 5a | Discrepancy acceptance | Keep (admin + manager, separation of duties) | Unchanged; also covers rejected lines. |
| 5b | Pending-QC outside quarantine | Not allowed | Unchanged. |
| 5c | Merging / repacking | Repacking needed | Split / repack into new labelled bags (existing feature); merging is not provided. |
| 5e | Failed optional test | Does not block release | `lab.optional_failures_block_release = false`; required tests must pass. |
| 5f | Re-use after expiry | Yes, expiry can be extended | A release decision may set a **new expiry date** (and next retest date); releasing an expired / retest-overdue bag requires them. Old and new dates are audited. |
| 5g | FIFO / FEFO override | Admin + manager | Unchanged. |
| 5h | Partial issue | Not allowed | `issue.whole_packages_only = true` (and the material default): only whole bags are suggested, reserved and issued. A request line that cannot be met exactly with whole bags stays partially issued and can be closed. |
| 5i | Retention | Audit events and documents 1 year, lab records 3 years | `retention.audit_days = 365`, `retention.document_days = 365`, `retention.lab_days = 1095`; `manage.py apply_retention` (dry run, then `--apply`) — see RUNBOOK. Lab purge covers completed / cancelled samples with their runs, results, decisions and reports; bags keep their QC status. |
| 6 | What an issue is tied to | Material request only | `issue.require_request = true`; issues without a request are refused. |
| 8 | Green tag | Both physical label and digital status | Existing behaviour (release tag print + QC status). |
| 9 | Notification channels | In-app only | Existing behaviour. |
| 7 | Labels, printer, weighing | 100 × 60 mm sticker label with barcode, printed through the browser; a small, low-cost label printer; pre-glued sticker labels; weights typed in kg | Labels carry a Code 128 barcode (USB scanners) plus a QR code (phone / tablet camera) and print black-on-white, so any 4-inch label printer works. The green release tag uses the same layout with a black "QC RELEASED" band — load green sticker stock for it. Gate weights default to kg; no weighbridge integration. |
| 10 | Users / volumes | No targets for now; show the measured data | *Usage analysis* page (`/usage`, API `GET /reports/usage-analysis/`) for admins, managers and auditors: accounts and active people by role, peak people working in one hour, activity by hour of day, daily receipts / bags / samples / issues / movements / scans / prints with averages and busiest day, warehouses, locations and data held. Observations only. |
| 11 | ERP / PO integration | Not planned | None. |

All of these are settings in *System configuration* (Django admin), except the material defaults, so they can be changed later without a deploy.

No business questions are open. New questions from testing should be added here.
