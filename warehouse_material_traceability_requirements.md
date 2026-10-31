# Warehouse Material Traceability System

## Product Requirements and Agent Development Guide

**Version:** 1.0\
**Status:** Initial requirements baseline\
**Application type:** Internal warehouse, quality-control, and material
traceability system\
**Required stack:** Django + Django REST Framework, Next.js, PostgreSQL

------------------------------------------------------------------------

## 1. Purpose

Build a web application that tracks incoming materials from truck
arrival and gate weighing through invoice registration, unloading
verification, storage, laboratory testing, approval/rejection, and issue
to the factory.

Every material line and every physically tracked bag/package must be
traceable throughout its lifecycle. Authorized users should be able to
see current status, past events, responsible users, quantities,
locations, and laboratory outcomes from the UI.

The application must support **first-in, first-out (FIFO)** stock issue,
with expiry-sensitive materials handled using **FEFO (first-expiry,
first-out)** where an expiry date is available.

## 2. Core principles

1.  **Traceability:** Every material receipt, bag/package, sample, test
    result, movement, and issue must have a unique identifier.
2.  **Auditability:** Important actions must create immutable
    audit/event records. Do not silently overwrite history.
3.  **Quantity integrity:** Received, accepted, rejected, sampled,
    available, reserved, and issued quantities must reconcile.
4.  **Quality gate:** Material must not be issued for production unless
    it has an explicit permitted quality status.
5.  **Role-based access:** Users may only perform actions permitted for
    their role and assigned workflow stage.
6.  **No silent data loss:** Use database transactions, validation,
    backups, and recoverable error handling.
7.  **Operational visibility:** Dashboard counts and detail pages must
    reflect persisted backend state, not frontend-only assumptions.
8.  **Configurable rules:** Units, material categories, storage areas,
    test types, and approval rules should be configurable rather than
    hard-coded wherever practical.

## 3. End-to-end business process

### Stage 1 --- Truck arrival and gate entry

A gate operator registers the arriving truck and receipt.

Capture at least: - Receipt / inward reference (system-generated,
unique) - Invoice number and invoice date - Supplier / source - Truck /
vehicle registration number - Arrival date and time - Gross, tare, and
net weight, where the weighing process provides them - Weight unit and
weighing method / weighbridge reference - Purchase order or delivery
challan reference, if applicable - Uploaded invoice, delivery challan,
weighment slip, and other documents - Notes and operator identity

**Validation and rules** - Invoice number must not be assumed globally
unique across all suppliers. Use a configurable duplicate check,
normally supplier + invoice number + invoice date. - Net weight should
be calculated as gross minus tare when both readings exist. Validate
that gross is not less than tare; allow authorized correction with a
reason and audit record. - Do not infer the quantity of each product
from truck weight unless the business explicitly configures that rule. -
A receipt can contain multiple invoice lines. - Save a draft before
submission; submitted gate data should be changed only through an
authorized correction workflow. - Record the time and user for each
status transition.

### Stage 2 --- Invoice line entry

For every invoice line, record: - Material code, if available - Material
name and description / grade - Invoice line number - Quantity and unit
of measure - Size / dimensions / specification - Declared weight and
weight unit, if applicable - Batch / lot number, if supplied -
Manufacturing date and expiry / retest date, if applicable - Source /
supplier - Required lab tests or quality specification, if known -
Remarks

The system must support multiple lines, different units, and multiple
packages for one line.

**Important modelling rule:** An invoice line is not necessarily the
same thing as a physical bag. Keep separate records for the invoice
line, the receipt quantity, and the individual package/bag. This allows
one line to produce many traceable bags.

### Stage 3 --- Unloading and physical verification

The unloading / receiving worker opens the pending receipt queue and
compares the invoice lines with the actual material.

For each line, capture: - Expected quantity and unit - Physically
received quantity and unit - Accepted quantity - Short / excess
quantity - Damaged quantity - Mismatched / unidentified quantity -
Observed size, grade, or description differences - Unloading start/end
time - Worker identity - Verification notes and photographs /
attachments where useful

The worker records discrepancies instead of silently changing invoice
values. Discrepancies should be flagged for authorized review.

**Completion conditions** - Each line is verified or explicitly marked
as discrepant. - Each physically tracked package receives a unique
package ID. - A receipt should not be marked fully unloaded while
unresolved quantities remain, unless an authorized user records an
exception.

### Stage 4 --- Package ID and barcode / QR label

Generate a unique, non-reusable ID for each physical bag/package. Print
a barcode or QR label containing the package ID or a secure URL/token
that resolves to its record.

Each package record should include: - Package ID (unique and
immutable) - Parent receipt and invoice line - Material and supplier -
Quantity and unit in the package - Batch / lot, when available - Size /
grade - Receipt and package creation timestamps - Current status -
Current storage location - Lab / sample status - Label print history and
reprint reason

**Label rules** - Do not encode sensitive data or mutable status
directly into the QR code. Store the stable package identifier or opaque
lookup token. - Scanning must resolve to the current server-side
record. - Label reprints must not create a new package ID. Record who
reprinted it and why. - Prevent accidental duplicate package creation
during retries or double-clicks. - If a package is split, merged,
repacked, or relabelled, record the parent-child relationship and
quantity movement. Never erase the original history.

### Stage 5 --- Put-away and warehouse storage

After physical verification and lab-sampling requirements are addressed,
the worker scans the package and assigns a storage location.

Support a hierarchy such as: **Warehouse → Zone → Rack → Shelf / Bin**

Record: - Package ID - From/to location - Quantity moved - Movement
date/time - Worker - Movement reason - Notes and condition, if relevant

The system must show current location and a chronological movement
history. A package must not be assigned to two active locations at once.
Location changes should be transactional.

Materials awaiting laboratory approval must be stored in a clearly
identified **Quarantine / Pending QC** area or equivalent status.
Physical storage and quality approval are separate concepts.

### Stage 6 --- Laboratory sampling and testing

Authorized lab personnel create a sample record linked to one or more
relevant package IDs / lots, according to configured sampling policy.

Capture: - Unique sample ID - Related package(s), material, receipt,
batch / lot - Sampled quantity and unit - Sample date/time and sampler -
Requested tests / specification - Laboratory and analyst - Sample
condition and notes - Test result values, units, and limits - Supporting
report / certificate attachments - Review / approval identity and
timestamps

Use configurable test definitions where possible: - Test name and
method - Expected unit - Minimum / maximum or qualitative acceptance
criteria - Required / optional indicator - Result and interpretation

**QC statuses** - `NOT_REQUIRED` --- only when policy explicitly permits
it - `PENDING_SAMPLING` - `SAMPLED` - `TESTING` - `PASSED` - `FAILED` -
`ON_HOLD` - `RETEST_REQUIRED`

Do not use one ambiguous boolean field for all laboratory states. Store
results and review history, not just the final status.

**Quality rules** - A sample result must be linked to the sample and its
source material. - Only authorized lab roles can enter results; only an
authorized reviewer can approve results if a review step is
configured. - A failed, held, or pending-QC package cannot be issued to
production. - Retesting must create a new test attempt or result
revision with history; never delete the original failed result. - If the
lab tests a representative sample for a lot, explicitly configure which
packages / lot quantities the result covers. Do not automatically
approve unrelated packages.

### Stage 7 --- Quality disposition and release

After testing and any required review, an authorized user records the
disposition: - Released / approved - Rejected - On hold - Retest
required - Partially accepted, if the business supports partial lot
disposition

Released material may receive a green physical tag or label. A green tag
is a visual indicator only; **the backend quality status is
authoritative**.

A green tag must not be issued or reprinted as a release signal unless
the server confirms that the material is released. Rejected / held stock
should be visibly distinguished and blocked from production issue.

### Stage 8 --- Issue to factory / consumption

The warehouse operator selects material for an authorized factory
request or issue transaction.

Issue record: - Unique issue / material requisition number - Factory,
department, production order, or cost centre - Requested material,
quantity, and unit - Package IDs / lots selected - Actual issued
quantity - Issue date/time - Issuing user and receiving user - QC status
at issue time - Notes and supporting reference

**FIFO / FEFO rules** - Default to oldest eligible stock first, based on
the configured receipt / availability timestamp. - If expiry or retest
dates apply, use FEFO: eligible stock with the earliest expiry / retest
deadline first. - Only eligible, released stock is available for
selection. - Never silently issue stock that is on hold, failed,
rejected, expired, recalled, or otherwise blocked. - Show a clear
override warning and require elevated permission plus a reason for any
policy override. If policy forbids override, reject it completely. -
Support partial package issue only if the material and packaging rules
permit it. Record remaining quantity and preserve traceability. - Use
database transactions and row locking or equivalent concurrency control
to prevent two users issuing the same stock simultaneously.

### Stage 9 --- Returns, adjustments, and disposal

Provide controlled workflows for: - Unused material returned from
factory - Damaged material - Expired material - Stock count
corrections - Supplier returns - Disposal / scrapping - Recall or
quality hold

Each adjustment requires a reason, quantity, actor, timestamp, and
reference. Returned stock must not automatically become available; it
may require inspection or QC clearance.

## 4. Required dashboards and screens

### A. Gate / inward dashboard

-   Trucks currently arrived / awaiting unloading
-   Receipts created today / this week
-   Receipts awaiting invoice line entry
-   Unloading pending / in progress / completed
-   Weight and invoice discrepancy alerts
-   Search by receipt, invoice, supplier, truck number, material, or
    date
-   Receipt details with document attachments and full timeline

### B. Unloading / receiving dashboard

-   Queue of receipts awaiting verification
-   Invoice expected vs actual quantity
-   Per-line discrepancy capture
-   Package creation and barcode / QR printing
-   Scan-to-open package workflow
-   Put-away tasks and completion state
-   Mobile/tablet-friendly layout for warehouse floor use

### C. Warehouse stock dashboard

-   Total stock by material, lot, location, and unit
-   Stock split by QC status: quarantine, pending, released, rejected,
    on hold
-   Available, reserved, and issued quantities
-   Ageing buckets (for example 0--30, 31--60, 61--90, 90+ days;
    configurable)
-   Near-expiry / retest alerts
-   Low-stock alerts if thresholds are configured
-   Current location and movement history
-   FIFO / FEFO issue suggestions

### D. Laboratory dashboard

-   Samples awaiting collection
-   Samples awaiting tests
-   Tests in progress
-   Results awaiting review
-   Passed / failed / held results
-   Retest queue and overdue test alerts
-   Sample-to-package traceability

### E. Material availability dashboard

For each material / package / lot, show: - Material name, code, size /
grade - Supplier and source - Receipt / invoice reference - Package ID
and batch / lot - Quantity and unit - Received / stored date and stock
age - Current location - QC status and test summary - Expiry / retest
date - Availability for issue - Issue / consumption history

### F. Management dashboard

-   Inward volume by date / supplier / material
-   QC pass / fail rates
-   Average time from gate to put-away
-   Average lab turnaround time
-   Stock ageing and near-expiry exposure
-   Reconciliation discrepancies
-   Material issued / consumed over time
-   Audit and exception summaries

### G. Search and traceability

A global search should support receipt number, invoice number, truck
number, supplier, material, package ID, barcode / QR, sample ID, batch /
lot, issue number, and factory order.

A package detail page should provide a chronological lifecycle timeline
from gate receipt to current state, including all scans, movements,
tests, approvals, issues, returns, and adjustments.

## 5. Roles and permissions

Use Django authentication and server-enforced role-based access control.
Suggested roles:

-   **Administrator:** user and configuration management; restricted,
    audited emergency corrections
-   **Gate Operator:** create gate receipt, capture weights, upload
    documents
-   **Receiving / Unloading Worker:** verify physical quantities, record
    discrepancies, create packages, print labels
-   **Warehouse Operator:** put-away, transfer, stock counts, issue and
    return
-   **Lab Technician:** receive samples, enter test results
-   **Lab Reviewer / Quality Manager:** review results, set disposition,
    release or reject material
-   **Factory / Requester:** raise material requests and acknowledge
    receipt, if required
-   **Auditor / Read-only:** view records, reports, and audit history
    without changing operational data
-   **Manager:** dashboards and reports, with no implied permission to
    alter technical records

Permissions must be checked in the backend API, not only by hiding
frontend buttons. Enforce separation of duties for sensitive actions
where configured (for example, result entry versus approval).

## 6. Suggested data model

Use normalized relational tables with foreign keys, indexes,
constraints, and explicit units. The agent should refine this design
before implementing migrations.

### Master data

-   `User` / Django auth user and role assignments
-   `Supplier`
-   `Material`
-   `UnitOfMeasure` and conversion rules
-   `Warehouse`
-   `StorageLocation`
-   `LabTestDefinition`
-   `Factory` / `Department`
-   `PurchaseOrder` (optional integration)
-   `SystemConfiguration`

### Receiving

-   `GoodsReceipt` --- inward reference, supplier, invoice, truck,
    weights, timestamps, status
-   `ReceiptDocument` --- uploaded file metadata and document type
-   `InvoiceLine` --- material, declared quantity/unit,
    size/specification, lot/date details
-   `ReceiptLineVerification` --- expected vs actual values, discrepancy
    and verification state

### Inventory and traceability

-   `Package` --- unique package ID, parent invoice line, quantity/unit,
    lot, status, current location
-   `PackageLabelEvent` --- print/reprint events
-   `InventoryMovement` --- immutable movement ledger with from/to,
    quantity, reason, actor, time
-   `StockAdjustment` --- controlled correction, reason, approval, and
    audit linkage
-   `StockReservation` --- optional reservation for a factory request
-   `MaterialRequest`
-   `MaterialIssue` and `MaterialIssueLine`
-   `MaterialReturn`
-   `DisposalRecord` / `SupplierReturn`

### Quality

-   `LabSample`
-   `SamplePackage` --- many-to-many relationship where a sample covers
    multiple packages
-   `LabTestRun`
-   `LabTestResult`
-   `QualityDisposition`
-   `QualityHold` / `Recall`, if needed

### Governance

-   `AuditEvent` --- actor, action, entity type/id, timestamp,
    request/correlation ID, reason, and safe before/after summary
-   `Notification` --- in-app alerts
-   `IdempotencyKey` or request token for retry-safe creation
    operations, where appropriate

### Data-model constraints

-   Package IDs, receipt references, sample IDs, and issue references
    must be unique.
-   Quantities must be non-negative and use suitable decimal precision;
    do not use floating-point for inventory quantities.
-   Every quantity must have a unit. Conversions must be explicit and
    controlled.
-   Use timezone-aware timestamps.
-   Store original invoice-declared values separately from verified
    values.
-   Prefer an append-only movement ledger. Current stock may be cached
    or derived, but it must reconcile with the ledger.
-   Use foreign keys and suitable `PROTECT` / restricted deletion
    policies for transactional records. Do not hard-delete records that
    have business history.
-   Define indexes for common lookups: package ID, invoice number,
    receipt reference, supplier, material, lot, QC status, location,
    dates, and issue reference.
-   Decide and document whether stock is controlled per individual
    package, per lot, or both. Preserve package-level traceability where
    required.

## 7. State machines and transition rules

Use explicit statuses and validated transitions instead of allowing
arbitrary status edits.

Suggested receipt states:
`DRAFT → GATE_REGISTERED → AWAITING_UNLOADING → UNLOADING_IN_PROGRESS → VERIFICATION_PENDING/EXCEPTION → UNLOADED → PUTAWAY_IN_PROGRESS → STORED → CLOSED`

Suggested package states may include:
`CREATED → AWAITING_PUTAWAY → IN_QUARANTINE → QC_PENDING → RELEASED → RESERVED → PARTIALLY_ISSUED → ISSUED`

Alternative terminal / blocking states: `REJECTED`, `ON_HOLD`,
`EXPIRED`, `RECALLED`, `RETURN_PENDING`, `DISPOSED`

These are illustrative, not a requirement to force every package through
every state. Model **physical location**, **quality disposition**, and
**inventory availability** as separate dimensions where possible. For
example, a package can be physically stored but still be on QC hold.

All transitions must: - Be validated server-side - Check user
permissions and required fields - Record actor and timestamp - Create an
audit event - Be atomic with related inventory changes - Return useful
validation errors to the UI

## 8. Technical architecture

### Backend

-   Python with a supported Django release
-   Django REST Framework for REST APIs
-   PostgreSQL as the system of record
-   Django migrations for schema changes
-   Django permissions / role checks
-   API pagination, filtering, ordering, and consistent error responses
-   Background jobs only where justified (notifications, report
    generation, scheduled expiry alerts); core stock transactions must
    not depend on an eventually consistent background task
-   OpenAPI / Swagger API documentation
-   File storage configured separately from the database where
    appropriate; store file metadata and access controls in PostgreSQL

### Frontend

-   Next.js with TypeScript
-   Responsive UI for desktop, tablet, and warehouse-floor use
-   Role-specific dashboards and navigation
-   Forms with client-side usability validation and authoritative
    backend validation
-   Search, filters, pagination, loading / empty / error states
-   Scan barcode / QR using a supported scanner or device camera where
    feasible; allow keyboard-wedge USB scanners
-   Label printing with a defined label size and printer workflow
-   Status badges, discrepancy alerts, confirmation dialogs, and event
    timeline
-   Do not rely on local browser state as the source of truth for
    inventory

### Integration boundary

-   Next.js communicates with Django APIs over authenticated HTTPS.
-   Django owns business rules, permissions, state transitions, quantity
    calculations, and stock transactions.
-   PostgreSQL is accessed by Django, not directly from the browser.
-   Keep secrets in environment variables / secret management, never in
    source control.
-   Use a consistent API version prefix such as `/api/v1/`.

## 9. API areas to plan

Exact endpoint names should follow the final API convention, but
cover: - Authentication, current user, roles - Suppliers, materials,
units, locations, test definitions - Goods receipts and receipt
documents - Invoice lines and unloading verification - Package creation,
lookup, QR / barcode resolution, label events - Put-away and inventory
movements - Samples, test runs, results, review, and disposition - Stock
availability, ageing, expiry, FIFO / FEFO suggestions - Material
requests, reservations, issues, returns, adjustments - Dashboards and
reports - Audit history, notifications, and exports

Use consistent response shapes, pagination, validation errors, and
permission-denied responses. Add idempotency protection for endpoints
where a retry could create duplicate receipts, packages, or issues.

## 10. Security, privacy, and operational safeguards

-   Use HTTPS outside local development.
-   Apply CSRF protection if using cookie-based session authentication;
    choose and document the authentication model before coding.
-   Set secure cookie flags and appropriate CORS rules.
-   Validate uploads by type and size; do not trust filenames or
    browser-provided content types.
-   Restrict access to invoices, lab reports, and other uploaded files.
-   Protect against SQL injection, XSS, CSRF, insecure direct object
    references, and unauthorized status changes.
-   Apply least-privilege database credentials.
-   Keep credentials, API keys, and production data out of logs.
-   Audit sensitive operations, including quality release, stock
    adjustment, label reprint, and FIFO/FEFO override.
-   Configure automated PostgreSQL backups, off-host backup storage,
    retention, and periodic restore tests before production.
-   Provide health checks, structured logs, error monitoring, and
    documented recovery procedures.
-   Use a staging environment and tested database migrations before
    production deployment.

## 11. Notifications and alerts

Provide in-app alerts (and optional email / other channels if
configured) for: - Invoice / received quantity mismatch - Damaged or
unidentified materials - Put-away overdue - Sample awaiting collection
or testing - Result awaiting review - Failed test, hold, or rejection -
Near-expiry / overdue retest - Stock below configured threshold -
Unusual or overridden inventory movement - Failed scheduled job or
backup verification

Alerts must link to the relevant record and respect user permissions.

## 12. Reporting and exports

Provide filterable reports for: - Inward register - Supplier-wise
receipts and discrepancies - Invoice vs actual receipt reconciliation -
Current stock by material, lot, package, QC status, and location - Stock
ageing and expiry / retest exposure - Lab test register and pass/fail
history - Material issue and consumption register - Package lifecycle /
traceability report - Stock adjustments, returns, disposal, and audit
events

CSV / Excel export may be provided to authorized users. Exports should
respect the same permissions and filters as the UI.

## 13. Non-functional requirements

-   **Reliability:** A failed operation must not leave stock partially
    updated.
-   **Concurrency:** Concurrent scans, put-away, issue, and adjustments
    must not oversell or double-issue stock.
-   **Performance:** Index frequently filtered fields; paginate large
    lists; avoid loading millions of records into the browser. Set
    measurable performance targets after expected user count and data
    volume are confirmed.
-   **Scalability:** Design for increasing receipts, packages, lab
    records, and audit events.
-   **Accessibility:** Clear labels, keyboard navigation, adequate
    contrast, and understandable validation messages.
-   **Usability:** Minimize typing on the warehouse floor; support
    scanner-first workflows and clear status indicators.
-   **Maintainability:** Separate domain/business logic from views and
    UI components; use typed API contracts and automated tests.
-   **Localization:** Store timestamps consistently and display them in
    the configured business timezone. Make date, number, and unit
    formats consistent.
-   **Retention:** Define retention and archival rules for audit events,
    documents, and lab records with the business.

## 14. Testing requirements

Write automated tests for at least: 1. Gate receipt creation and
duplicate invoice warning. 2. Gross/tare/net validation and weight
correction audit. 3. Multiple invoice lines and package generation. 4.
Retry-safe package creation and unique ID enforcement. 5. Expected vs
actual quantity mismatch. 6. Package location movement and movement
history. 7. Sample-to-package / lot association. 8. Lab pass, fail,
hold, retest, and approval permissions. 9. Blocked issue for pending,
failed, rejected, expired, recalled, or held material. 10. FIFO and FEFO
ordering. 11. Concurrent issue attempts against the same stock. 12.
Partial package issue and remaining balance. 13. Return, adjustment, and
disposal audit. 14. Role-based API access and object-level permissions.
15. Dashboard totals reconciling with inventory records. 16. File upload
validation and protected downloads. 17. Migration and backup/restore
smoke tests.

Use realistic test fixtures and transaction-aware tests for inventory
concurrency. Include frontend tests for important forms, scan workflows,
permissions, and error states.

## 15. Acceptance criteria

The first production-ready release is acceptable only when: - A user can
register a truck receipt with invoice and weight details. - Multiple
invoice lines can be recorded under one receipt. - Actual unloading
quantities and discrepancies are captured without overwriting declared
invoice values. - Every physical package has a unique, scannable ID
linked to its source receipt and invoice line. - A package's current
location and full movement history are visible. - Lab samples and
results are traceable to the correct package / lot. - Pending, failed,
rejected, held, expired, and recalled materials cannot be issued through
the normal workflow. - Authorized quality disposition controls
release. - FIFO / FEFO recommendations select eligible stock in the
configured order. - Issues, returns, and adjustments update stock
atomically and are auditable. - The dashboards show live persisted data
and useful filtering/search. - Users cannot perform actions outside
their role by calling the API directly. - Automated tests cover the
critical business rules and pass in CI. - Backup and restore procedures
have been tested and documented.

## 16. Suggested implementation phases

### Phase 0 --- Clarify business decisions

Confirm material units and conversions, package quantity rules, whether
invoice weight is line-level or truck-level, sampling policy,
lot-vs-package QC scope, expiry / retest rules, label printers, weighing
scale integration, user roles, and required reports.

### Phase 1 --- Foundation

Set up repositories, Django, DRF, PostgreSQL, Next.js TypeScript,
environment configuration, authentication, roles, master data,
migrations, API documentation, logging, and CI.

### Phase 2 --- Gate and receipt

Implement receipt registration, weight capture, invoice lines,
attachments, duplicate warnings, receipt dashboard, and audit history.

### Phase 3 --- Unloading and labels

Implement verification, discrepancy handling, package generation, QR /
barcode lookup, label printing, and scan-friendly UI.

### Phase 4 --- Warehouse and inventory

Implement locations, put-away, transfers, movement ledger, stock
balances, ageing, and reconciliation.

### Phase 5 --- Laboratory and release

Implement samples, test definitions, result entry, review, disposition,
holds, retests, and release controls.

### Phase 6 --- Factory issue and FIFO / FEFO

Implement requests, eligibility checks, allocation, reservations if
required, issue, partial issue, returns, and transactional concurrency
safeguards.

### Phase 7 --- Reports, hardening, and deployment

Complete dashboards, exports, notifications, performance checks,
security review, backup/restore testing, monitoring, and production
runbooks.

Build in small vertical slices, with migrations, backend tests, API
documentation, and usable UI for each phase. Do not build every screen
using mocked data and postpone the business rules until later.

## 17. Instructions to the coding agent

1.  Read this document before implementing features and treat it as the
    baseline product specification.
2.  First inspect the existing repository and report its current
    structure, dependencies, and implementation state. Do not overwrite
    existing work blindly.
3.  Produce a short implementation plan and identify unresolved business
    decisions. Use safe, documented defaults for non-blocking choices;
    ask the project owner when a decision changes inventory, quality, or
    compliance behavior.
4.  Define the domain model and status transitions before building the
    UI.
5.  Keep business logic in the Django service/domain layer and enforce
    it in API transactions. Frontend checks are for usability only.
6.  Use migrations, seed/demo data that is clearly marked as
    non-production, and automated tests.
7.  Never use mock data as if it were production data. If a screen
    temporarily uses fixtures, label that clearly and replace it before
    declaring the feature complete.
8.  Do not hard-code supplier names, materials, storage locations, test
    thresholds, expiry policy, or user roles when these should be
    configurable.
9.  Do not delete or rewrite audit history to correct mistakes. Create a
    compensating correction event.
10. After each phase, report files changed, migrations added, tests run
    and results, API endpoints implemented, remaining gaps, and how to
    run the feature locally.
11. Keep setup instructions current in `README.md` and maintain
    `.env.example` without real secrets.
12. Do not claim a feature is complete until its backend rules,
    permissions, UI states, tests, and relevant acceptance criteria are
    satisfied.

## 18. Open questions to confirm with the business

These affect final schema and workflow design: - Are materials tracked
by individual bag, pallet, drum, bundle, or another package type? - Does
each package have its own quantity, or can one package contain multiple
units? - Are truck weights used only for reconciliation or allocated to
individual invoice lines? - Which units and conversions are required
(kg, tonne, pieces, litres, dimensions, etc.)? - Can a lab sample
approve a complete supplier lot, a receipt line, or only individual
packages? - Can one invoice line be partially accepted or rejected? -
Are expiry dates, shelf life, or retest dates applicable to all
materials or only selected categories? - Is factory issue tied to a
material request, production order, or just an authorized issue slip? -
Which barcode symbology, label size, printer, scanner, and weighing
scale integrations are needed? - Is green tagging a physical label, a
digital status, or both? - Are notifications required by email, SMS, or
only in-app? - How many users, warehouses, packages per day, and
concurrent operators are expected? - Are purchase orders, ERP,
accounting, or supplier systems to be integrated later?

------------------------------------------------------------------------

**Critical design reminder:** Physical location, quality disposition,
and inventory availability should be modelled distinctly. A package can
be physically in storage while quarantined, and a released package can
still be unavailable because it is reserved or already issued. The
application must never infer that "stored" means "approved for use."
