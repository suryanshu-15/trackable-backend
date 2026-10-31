# Trackable — Functionality checklist

Everything the application does today, grouped by area, with who does it and where. Use the
**Check** column while testing (✔ works / ✘ problem — note what happened).

Sign in at http://localhost:3000 as `demo_<role>` with password `demo-pass-123!`
(roles: `admin`, `gate`, `receiving`, `warehouse`, `lab_tech`, `lab_reviewer`, `factory`, `auditor`, `manager`).
Each user lands on their own dashboard and sees only their menu. Every rule below is enforced by the
server, so a hidden button is never the only protection.

**Demo data** (all names start with `[DEMO]`):

| Invoice | State | Good for testing |
|---|---|---|
| DEMO-INV-A | 1 t in 4 bags of 0.25 t, each sampled and released, on rack A-R1 | issuing, FIFO, holds, release tag |
| DEMO-PO-1001 | Factory request for 0.75 t: one bag issued, two bags (0.5 t) reserved | issuing the rest, acknowledgement, returns |
| DEMO-INV-B | 2 bags in quarantine, one sample per bag in testing | lab results and decisions |
| DEMO-INV-C | Truck with 2.4 t steel billets awaiting unloading | unloading, verification, bags, labels, put-away |
| DEMO-INV-D | Gate draft with no lines | invoice lines, gate registration |

---

## 1. Sign-in, navigation, search

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 1.1 | Sign in / sign out; session ends after sign-out | all | login page, top bar | |
| 1.2 | Too many wrong passwords from one place are blocked for a minute | all | login page | |
| 1.3 | Role-specific menu and landing page | all | left menu | |
| 1.4 | Global search: receipt, invoice, truck, supplier, material, bag ID, scanned QR, sample, lot, issue, request, production order | all (factory sees only its own items) | search box, top bar | |
| 1.5 | Notifications with unread count; open the linked record; mark read | all | bell icon | |
| 1.6 | Works on desktop, tablet and phone screens | all | any page | |

## 2. Gate (truck arrival)

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 2.1 | Gate dashboard: trucks on site, receipts today / this week, awaiting line entry, unloading pending / in progress / done, exceptions | gate, managers | Gate & receipts | |
| 2.2 | Weight and quantity discrepancy alerts | gate, managers | Gate & receipts | |
| 2.3 | Receipt list with search and filters (status, supplier, material, dates) | all except factory | Gate & receipts | |
| 2.4 | New gate receipt: supplier, invoice, truck, driver, arrival, gross / tare in **kg**, net computed | gate | Gate & receipts → New gate receipt | |
| 2.5 | Duplicate invoice warning; "Create anyway" is recorded | gate | new receipt form (reuse DEMO-INV-A) | |
| 2.6 | Edit a draft receipt | gate | receipt page → Edit | |
| 2.7 | Invoice lines: add / edit / delete, material, quantity in **t**, lot, **expiry and retest dates** | gate, receiving | receipt page | |
| 2.8 | Register at gate → send for unloading (needs at least one line) | gate | receipt page buttons | |
| 2.9 | Correct weights after registration, with reason (original kept in history) | gate, admin | receipt page → Correct weights | |
| 2.10 | Upload documents (invoice, challan, weighment slip …); protected download | gate, receiving | receipt page → Documents | |
| 2.11 | Full receipt history | all except factory | receipt page → History | |
| 2.12 | Cancel receipt with reason | admin | receipt page | |

## 3. Unloading, bags and labels

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 3.1 | Unloading queue (truck cards), labels to print, put-away tasks | receiving, warehouse | Unloading & put-away | |
| 3.2 | Start unloading | receiving, warehouse | receipt page | |
| 3.3 | Verify a line: **accept the whole line** (quantity in t) or **reject the line** with a reason — no partial acceptance | receiving, warehouse | receipt page → Verify | |
| 3.4 | Short / excess against the invoice shown and flagged; receipt moves to Exception | receiving | receipt page | |
| 3.5 | Manager accepts a discrepancy or rejection (not the person who verified) | manager, admin | receipt page → Accept discrepancy | |
| 3.6 | Create bags: equal bags or individual quantities, lot / expiry / retest; cannot exceed accepted quantity; safe to retry | receiving, warehouse | receipt page → Create packages | |
| 3.7 | Print labels: 100 × 60 mm sticker with BAG, barcode and QR code; print is recorded only after you confirm | receiving, warehouse | after creating bags, or bag page → Print label | |
| 3.8 | Reprint keeps the same bag ID and needs a reason | receiving, warehouse | bag page → Reprint label | |
| 3.9 | Submit verification → mark unloaded (all accepted quantity bagged and labelled) | receiving | receipt page | |

## 4. Scanning, put-away and stock

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 4.1 | Scan to open a bag: USB scanner, phone / tablet camera, or typed ID; QR on the label opens the bag directly | all except factory | Scan | |
| 4.2 | Put away by scanning the bag, then scanning or choosing the location | receiving, warehouse | Unloading & put-away → Scan to put away | |
| 4.3 | Bags awaiting QC must go to quarantine (other locations refused) | receiving, warehouse | put-away dialog | |
| 4.4 | Receipt becomes Stored automatically when every bag is put away | system | receipt page | |
| 4.5 | Transfer a bag between locations | warehouse | bag page → Transfer | |
| 4.6 | Repack (split) a bag into new labelled bags with a reason | warehouse | bag page → Split / repack | |
| 4.7 | Bag page: quantity, location, lot, dates, QC, why it cannot be issued, samples, split history | all | Material availability → bag | |
| 4.8 | Bag lifecycle timeline, movement ledger, issues, label history | all | bag page tabs | |
| 4.9 | Stock dashboard: totals, QC split, available / reserved, awaiting put-away, near expiry / retest, low stock | warehouse, managers | Stock | |
| 4.10 | Stock balances grouped by material / lot / location / QC status / supplier | warehouse, managers | Stock | |
| 4.11 | Stock ageing (0–30, 31–60, 61–90, 91+ days) and near-expiry list | warehouse, managers | Stock | |
| 4.12 | FIFO / FEFO pick suggestion for a material and quantity | warehouse, managers | Stock | |
| 4.13 | Material availability list with filters (material, supplier, QC, status, available, expiring before) | all except factory | Material availability | |

## 5. Laboratory and quality

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 5.1 | Lab dashboard: bags awaiting sampling, awaiting collection, awaiting tests, in progress, awaiting review, overdue, retest queue, decisions in 30 days | lab, managers | Laboratory | |
| 5.2 | Request samples — **one sample per bag** (selecting several bags creates several samples) | lab tech, reviewer | Laboratory → select bags → Request sample | |
| 5.3 | Collect (optional quantity drawn from the bag), receive in lab, start testing | lab tech | sample page | |
| 5.4 | Enter results; pass / fail worked out by the server from the test limits; corrections kept as revisions | lab tech | sample page → Enter results | |
| 5.5 | Submit for review | lab tech | sample page | |
| 5.6 | Decision: release, reject, hold, retest — not by whoever entered or submitted the results | lab reviewer | sample page → Record decision | |
| 5.7 | Failed **optional** tests do not block release; failed / missing required tests do | lab reviewer | decision dialog | |
| 5.8 | Release an **expired** bag after retest by giving a new expiry date (and next retest date) | lab reviewer | decision dialog | |
| 5.9 | Lab reports / certificates: upload and protected download | lab | sample page | |
| 5.10 | Green release tag print (black "QC RELEASED" band, print on green stickers); only for released, unblocked bags | warehouse, lab reviewer | bag page → Release tag | |
| 5.11 | Holds and recalls by bag or by material / lot; impact view (what is in stock, what was already issued); release with notes | lab reviewer | Holds & recalls | |
| 5.12 | Expired, retest-overdue, failed, held or recalled bags can never be issued | system | bag page shows why | |

## 6. Requests and issuing to the factory

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 6.1 | Raise a material request (factory, department, production order, materials in t) | factory (or warehouse) | Material requests → New request | |
| 6.2 | Factory sees only its own requests and issues | factory | Material requests, Issues | |
| 6.3 | Allocate (reserve) stock in FIFO / FEFO order; re-allocate; release reservations | warehouse | request page | |
| 6.4 | Issue against a request only: suggested pick list or scan bags; **whole bags only** | warehouse | request page → Issue material | |
| 6.5 | Picking a newer bag than suggested is blocked; manager / admin can override with a reason (flagged, notified, audited) | warehouse → manager | request page | |
| 6.6 | Issue cannot exceed what is still requested; safe to retry | system | request page | |
| 6.7 | Factory acknowledges receipt | factory | Issues → issue → Acknowledge | |
| 6.8 | Cancel / close a request with a reason | warehouse (factory: cancel own) | request page | |
| 6.9 | Return from production → new bag, blocked until lab retest or inspection | warehouse | issue page → Receive return | |

## 7. Stock control

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 7.1 | Request a stock adjustment (count correction / increase / decrease) with reason | warehouse | Returns, disposals & adjustments → Adjustments | |
| 7.2 | Approve or reject an adjustment — not your own | manager, admin | same tab | |
| 7.3 | Disposal (scrap) or return to supplier with reason; scrapping usable stock needs a manager | warehouse, manager | Disposals tab | |
| 7.4 | Returns list | warehouse, managers | Returns tab | |
| 7.5 | Ledger reconciliation (bag balances vs movement history) | warehouse, managers | Ledger reconciliation tab | |

## 8. Management, reports and audit

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 8.1 | Management dashboard: inward by day / supplier / material, QC pass rate, gate → put-away time, lab turnaround, ageing, issues over time, exceptions | admin, manager, auditor | Management | |
| 8.2 | **Usage analysis**: people by role, peak people in one hour, activity by hour, daily workload averages and busiest days, data held — no targets | admin, manager, auditor | Usage analysis | |
| 8.3 | 14 reports with filters, shown by role: inward register, supplier receipts, receipt reconciliation, current stock, ageing, expiry, lab register, QC decisions, issue register, adjustments, returns, disposals, audit events, bag lifecycle | by role | Reports | |
| 8.4 | Export any report to CSV or Excel (same filters and permissions; exports are audited) | by role | report page | |
| 8.5 | Audit trail of every change, approval, override, print, scan and download, with details | admin, manager, auditor | Audit trail | |

## 9. Administration

| # | Function | Who | Where | Check |
|---|---|---|---|---|
| 9.1 | Users: add, change role, deactivate / activate | admin | Users | |
| 9.2 | Master data: suppliers, materials, units, warehouses, locations, lab tests, factories, departments | admin | Django admin, http://localhost:8000/admin/ | |
| 9.3 | Business rules (System configuration): tonnes, whole-line acceptance, one sample per bag, whole-bag issue, request required, FIFO override roles, retention periods, alert timings | admin | Django admin → System configuration | |
| 9.4 | Hourly alerts: near expiry / retest, overdue samples, overdue put-away, low stock, ledger mismatch | system (cron) | Notifications | |
| 9.5 | Retention: audit events and documents 1 year, lab records 3 years (report first, then apply) | system (cron) | `manage.py apply_retention` | |
| 9.6 | Backup, restore and restore check | operations | `scripts/` — see RUNBOOK | |

---

**Not included (by decision):** email / SMS notifications, weighbridge or scale integration, ERP / PO
integration, merging bags, partial issue of a bag, partial acceptance of an invoice line.
