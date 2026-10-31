# Trackable — End-to-end test guide

This guide walks one new truck of material through every stage of Trackable, from the gate to the
factory. For every screen it gives **values to type**, explains **what each field means**, and says
**what you should see**. Short extra scenarios at the end cover rejections, discrepancies, holds and
the other exception paths.

Open http://localhost:3000. Every account uses the password **`demo-pass-123!`**. Swap users with
**Sign out** in the top-right corner. If you swap users many times within a minute, sign-in is blocked
for a minute as brute-force protection; just wait and try again.

> Dates below assume testing on **31 Oct 2026**. If you test on another day, keep the same
> *distance* from today: expiry about 1 year ahead, retest about 6 months ahead.

---

## 0. Key ideas in two minutes

| Term | Meaning in Trackable |
|---|---|
| **Gross weight** | Weight of the **loaded truck** on the weighbridge as it arrives (truck + material + packaging). |
| **Tare weight** | Weight of the **empty truck** (weighed after unloading, or the truck's registered empty weight). |
| **Net weight** | Gross − tare = weight of what was delivered. Trackable calculates it; you never type it. |
| **Weight tolerance** | Net weight is compared with the weights declared on the invoice lines. A difference above **0.5 %** raises a *weight discrepancy* alert on the gate dashboard and the receipt. |
| **Invoice line** | One row of the supplier's invoice: one material, one quantity, one lot. A truck can carry several lines. |
| **Bag (package)** | One physical bag with its own ID (e.g. `PKG-9F6E0714B9`), label, location and history. Bags are counted in **tonnes (t)**. |
| **Lot / batch** | The supplier's production batch printed on the bag. Used for traceability and recalls. |
| **Expiry date** | After this date the material must not be used. Expired bags are blocked from issue. |
| **Retest date** | The date by which the lab must re-check the material. Past it, bags are blocked until retested. |
| **QC status** | Where the bag is in quality control: *Pending sampling → Sampled → Testing → Passed / Failed / On hold / Retest required*. Only **Passed** bags can be issued. |
| **Quarantine** | Location `Q`. Bags that are not yet passed (or failed) must be stored there. |
| **FIFO / FEFO** | First-In-First-Out (oldest receipt first) / First-Expiry-First-Out (earliest expiry or retest first). Trackable uses FEFO when bags have dates, which all yours do. |
| **Reservation** | Bags held for a specific factory request so nobody else can issue them. |

**Business rules in force:** bags are counted in tonnes; an invoice line is **accepted whole or
rejected**, never partly; **one lab sample per bag**; failed *optional* tests don't block release;
only **whole bags** are issued, and only **against a material request**.

### Users

| Username | Role | What they do in this test |
|---|---|---|
| `demo_gate` | Gate operator | Registers the truck and invoice |
| `demo_receiving` | Receiving / unloading | Unloads, verifies, creates bags, prints labels, puts away |
| `demo_lab_tech` | Lab technician | Samples bags, enters test results |
| `demo_lab_reviewer` | Lab reviewer / QA | Releases or rejects bags; holds and recalls |
| `demo_warehouse` | Warehouse operator | Moves stock, issues to factory, returns, adjustments, disposals |
| `demo_factory` | Factory / requester | Requests material, acknowledges receipt |
| `demo_manager` | Manager | Approves exceptions and overrides; dashboards and reports |
| `demo_auditor` | Auditor | Read-only: reports and audit trail |
| `demo_admin` | Administrator | Everything, plus users and master data |

### Master data you will pick from (already loaded)

| Kind | Values |
|---|---|
| Suppliers | [DEMO] Supplier Alpha Chemicals, [DEMO] Supplier Beta Polymers, [DEMO] Supplier Gamma Metals |
| Materials | **DEMO-MAT-001 Sodium carbonate** (lab tests: moisture, purity, appearance), DEMO-MAT-002 HDPE granules (moisture), DEMO-MAT-003 Steel billets (no lab testing) |
| Lab tests | Moisture content: required, pass if **0 – 0.5 %**. Purity: required, pass if **≥ 98.5 %**. Appearance: **optional**, pass if "White crystalline powder". |
| Locations | `Q` = quarantine; shelves `A-R1-S1` … `A-R1-S3`, `A-R2-S1` … `A-R2-S3` (zone A, racks 1–2, shelves 1–3) |
| Factory | [DEMO] Factory 1, departments [DEMO] Mixing and [DEMO] Extrusion |

---

## The main scenario

A truck from Alpha Chemicals delivers **2 t of sodium carbonate in 4 bags of 0.5 t**. Three bags pass
the lab and one fails. The factory then requests 1.25 t, which is issued, acknowledged and partly returned.

### Stage 1 — Gate entry  (`demo_gate`)

**Gate & receipts → New gate receipt**

| Field | Type this | What it is / how it's used |
|---|---|---|
| Supplier | [DEMO] Supplier Alpha Chemicals | Who sent the material. Used for traceability, supplier reports and the duplicate check. |
| Invoice number | `TEST-INV-1001` | The number on the supplier's invoice. Supplier + invoice number + invoice date must be unique; otherwise you get a duplicate warning. |
| Invoice date | 31 Oct 2026 | Date printed on the invoice (part of the duplicate check). |
| Purchase order | `PO-7788` | Your purchase order number (optional; for reference and searching). |
| Delivery challan | `DC-5521` | The delivery note number that came with the truck (optional). |
| Truck / vehicle number | `OD02AB4521` | Registration plate. It's uppercased automatically and searchable. |
| Driver name | `Ramesh Kumar` | Optional; for the gate record. |
| Arrival | now (pre-filled) | When the truck reached the gate. Used for "trucks on site" and for gate → put-away time. |
| Gross weight | `14205` | Loaded truck weight from the weighbridge, in kg. |
| Tare weight | `12200` | Empty truck weight, in kg. Must not exceed gross. The form shows **Net: 2005** under the field. |
| Weight unit | Kilogram (kg), pre-selected | Truck weights are recorded in kg. |
| Weighing method | Weighbridge | How the weight was obtained: weighbridge, platform scale, vendor declared or other. |
| Weighbridge slip / reference | `WB-1001` | Number on the weighbridge slip, so the weight can be traced to the paper. |
| Gate notes | `Seal intact` | Anything the gate operator noticed. |

Click **Create receipt**. The receipt page opens with status **Draft** and a number like `GR-20261031-XXXXXXXX`.

**Add the invoice line:** on the receipt page, **Invoice lines & verification → Add line**

| Field | Type this | What it is / how it's used |
|---|---|---|
| Material (master) | DEMO-MAT-001 — [DEMO] Sodium carbonate | Links the line to your material master, which decides lab tests and dates. Leave it empty only if the material is unknown; receiving can identify it later. |
| Description on invoice | (fills itself) | The text as printed on the invoice. Required if no master material is chosen. |
| Supplier material code | `SC-99` | The supplier's own product code (optional). |
| Grade / specification | `Light` | Grade printed on the invoice. Receiving compares it with what they see. |
| Declared quantity | `2` | Quantity on the invoice. |
| Unit | Tonne (t) | Bags are counted in tonnes. |
| Declared weight | `2000` | Weight on the invoice. It is compared with the truck's net weight (2005 kg here: 0.25 % difference, within tolerance). |
| Weight unit | kg | Unit of the declared weight. |
| Batch / lot | `LOT-SC-2610` | Supplier batch. Every bag inherits it; holds and recalls work by lot. |
| Manufactured | 15 Oct 2026 | Production date (optional). |
| Expiry | 31 Oct 2027 | **Required.** After this date the bags are blocked. |
| Retest date | 30 Apr 2027 | **Required.** The lab must re-check by this date. |
| Quality specification | `IS 251 Grade 1` | Optional text for the lab. |
| Remarks | — | Optional. |

Click **Add line**. Then click **Register at gate** (status → *Gate registered*) and **Send for unloading**
(status → *Awaiting unloading*). After this the lines are locked; only the gate or an admin can still
correct weights, with a reason.

**Also try:**
- **Correct weights:** change tare to `12205` with reason "Re-weighed empty". The history shows old and new values.
- **Documents:** upload a PDF or photo of the invoice (type *Invoice*), then download it.
- **Duplicate warning:** start another new receipt with the same supplier, `TEST-INV-1001` and today's date. A *Possible duplicate invoice* warning appears; click **Go back**.

**Check:** the gate dashboard shows the truck under *Trucks on site* and *Unloading pending*.

### Stage 2 — Unloading and verification  (`demo_receiving`)

**Unloading & put-away → click the truck card `OD02AB4521` → Start unloading**

Then click **Verify** on line 1:

| Field | Type this | What it is / how it's used |
|---|---|---|
| Accept whole line / Reject line | **Accept whole line** | The line is either accepted entirely or rejected entirely. Rejecting needs a reason and a manager's approval, and nothing from the line enters stock. |
| Received quantity | `2` | What was physically counted or weighed. If it differs from the invoice, the line is flagged as short or excess. |
| Unit | t (pre-selected) | Must be tonnes for bags. |
| Material actually identified | leave as *Same as invoice* | Set it only if the material on the truck is different from the invoice. That flags a discrepancy. |
| Observed description / grade | **leave empty** | Fill these only when what you see **differs** from the invoice; anything typed here is treated as a mismatch. |
| Discrepancy notes | — | Required when there is a discrepancy. |
| Verification notes | `4 bags, dry` | Free notes. |

Click **Record verification**. The line shows **Verified**.

**Create the bags:** click **Create packages** on the line.

| Field | Type this | What it is / how it's used |
|---|---|---|
| Equal bags / Individual quantities | Equal bags | Use *Individual quantities* when bags differ (e.g. `0.5, 0.5, 0.6, 0.4`). |
| Number of bags | `4` | One bag record and one label per physical bag. |
| Quantity each (t) | `0.5` | 4 × 0.5 = 2 t. The total cannot exceed the accepted quantity. |
| Batch / lot, Expiry, Retest date | pre-filled from the line | Change them only if a bag carries different markings. |

Click **Create packages**. Four bag IDs appear; click **Print labels**.

**Print labels page:**

| Field / button | What to do | Why |
|---|---|---|
| Printer | `ZEBRA-DOCK-1` (any name) | Recorded with the print, so you know which printer was used. |
| Print | Opens the browser print dialog: 100 × 60 mm, one label per page | Each label has BAG, the bag ID, a QR code, a barcode, material, lot, packed quantity, expiry and retest. |
| "Did the labels print correctly?" → **Yes, record print** | Confirm | Only a confirmed print is recorded, and the first print releases the bag for put-away. Without a printer, use **Already printed? Record it**. |

Back on the receipt, click **Submit verification** and then **Mark unloaded** (status → *Unloaded*).

### Stage 3 — Put-away  (`demo_receiving` or `demo_warehouse`)

**Unloading & put-away → Scan to put away**

1. In *Scan a package label*, type (or scan) the first bag ID, e.g. `PKG-…`, and press **Enter**.
2. In the dialog, *Scan location code*: type `Q` and press Enter (or pick "Quarantine" from *Location*).
   These bags are still waiting for the lab, so only quarantine locations are offered. Try `A-R2-S1` to see the refusal.
3. *Notes*: optional. Click **Confirm put-away**.
4. Repeat for all 4 bags.

**Check:** the receipt status becomes **Stored** by itself once every bag is put away. On a bag's page
(Material availability → bag), the *Lifecycle* tab shows everything so far.

### Stage 4 — Laboratory  (`demo_lab_tech`, then `demo_lab_reviewer`)

**Laboratory → Packages awaiting sampling:** tick all 4 bags of `LOT-SC-2610` → **Request sample (4)**

| Field | Type this | What it is / how it's used |
|---|---|---|
| Tests | Moisture, Purity, Appearance (pre-ticked from the material) | Which tests the lab must run. |
| Results required by | 2 Nov 2026 17:00 | Due time. After it, the sample shows as **Overdue** and an alert is sent. |
| Laboratory | `QC Lab 1` | Which lab does the work. |
| Specification | `IS 251 Grade 1` | Standard to test against (for reference). |
| Notes | — | Optional. |

Click **Request 4 samples**. Under the one-sample-per-bag rule, each bag gets its own sample.

For **each** sample (Samples list → open it):

1. **Collect sample:**
   - *Drawn from each package*: **leave empty** for this test. A drawn quantity is deducted from the bag; with whole-bag issue, the bag then no longer matches a request exactly.
   - *Total sample quantity*: `0.5`, *Unit*: kg. Informational: how much the lab took.
   - *Sample condition*: `Sealed, dry`.
2. **Receive in lab:** confirms the sample physically reached the lab.
3. **Start testing:** *Notes* optional. This opens test run 1.
4. **Enter results** (*Save results*):

   | Test | Bags 1–3 (pass) | Bag 4 (fail) | Meaning |
   |---|---|---|---|
   | Moisture content (%) | `0.21` | `0.80` | Above 0.5 % fails. The server decides pass / fail from the limits. |
   | Purity (%) | `99.2` | `99.0` | Must be ≥ 98.5 %. |
   | Appearance | `White crystalline powder` | `White crystalline powder` | Optional test; a different text would fail but not block release. |
   | Interpretation | optional | `Moisture high` | Your comment on each result. |

   Saving again creates a new revision; old values stay in the history.
5. **Submit for review:** locks the results.

Sign in as **`demo_lab_reviewer`**. Open each sample → **Record decision**:

| Field | Bags 1–3 | Bag 4 | What it is / how it's used |
|---|---|---|---|
| Decision | **Release / approve** | **Reject** | Release → QC *Passed* (can be issued). Reject → *Failed* (blocked; stays in quarantine). Hold → decide later. Retest → test again. |
| Next retest date | leave empty | — | Optional: moves the bag's retest date forward. Required if it has already passed. |
| New expiry date | leave empty | — | Only for bags that have **expired**: after a successful retest you can extend the expiry. Releasing an expired bag without it is refused. |
| Reason | `All tests within specification` | `Moisture 0.80 % exceeds 0.5 %` | Required; stored in the audit trail. |
| Notes | optional | optional | |

Bag 4's release is refused because a required test failed; the dialog explains why. Try recording a
decision as `demo_lab_tech` too: it's refused, because whoever enters results cannot approve them.

**Also try:** on a released bag's page, click **Release tag** to print the green "QC RELEASED" tag
(load green stickers in the real printer).

### Stage 5 — Move released bags to the shelf  (`demo_warehouse`)

For bags 1–3: open the bag (Scan, or Material availability) → **Transfer** → location `A-R2-S1`,
notes `Released stock` → **Confirm transfer**. They now show **Available** in Material availability.
Bag 4 (failed) must stay in quarantine.

### Stage 6 — Factory request  (`demo_factory`)

**Material requests → New request**

| Field | Type this | What it is / how it's used |
|---|---|---|
| Factory | [DEMO] Factory 1 | Who needs the material. |
| Department | [DEMO] Mixing | Which department (optional). |
| Required by | 1 Nov 2026 10:00 | When the material is needed. |
| Production order | `PO-TEST-2001` | The production order the material is for; searchable, and appears on the issue. |
| Cost centre | `CC-MIX-01` | For costing (optional). |
| Material / Quantity / Unit | DEMO-MAT-001 / `1.25` / t | What and how much. |
| Notes | — | Optional. |

Click **Submit request**. The factory user sees only their own requests.

### Stage 7 — Issue to the factory  (`demo_warehouse`, then `demo_manager`)

Open the request (Material requests → `PO-TEST-2001`).

- **Allocate stock** (optional): reserves bags in FEFO order so no one else can take them.
- **Issue material** section:
  - The *Suggested pick list* shows the bags to use, earliest expiry first. Expect the free 0.25 t demo bag
    of `DEMO-LOT-A1` (it expires sooner), then two of your 0.5 t bags: 0.25 + 0.5 + 0.5 = 1.25 t.
  - **FIFO test:** before using the suggestions, type one of **your** bag IDs into *Scan package to pick*
    (Enter) and click **Issue 1 package(s)**. You get *Out of FIFO / FEFO order*, because an older bag was
    skipped. As `demo_warehouse` you cannot override; this is correct.
  - Click **Use suggested picks**, which replaces the pick list. *Issue quantity* is locked to the whole bag.

  | Field | Type this | What it is / how it's used |
  |---|---|---|
  | Received by (name) | `Line lead – Suresh` | Person at the factory who took the material. |
  | Reference / gate pass | `GP-3301` | Gate pass or slip number (optional). |
  | Notes | — | Optional. |
  | Override reason | (only appears after an out-of-order pick) | A manager or admin explains why older stock was skipped; the issue is flagged and notified. |

  Click **Issue 3 package(s)**. An issue `ISS-…` opens.

- **Override test** (optional): as **`demo_manager`**, repeat the out-of-order pick on a new request and
  give an override reason. The issue shows the "FIFO override" badge.

### Stage 8 — Factory acknowledges  (`demo_factory`)

**Issues → open the issue → Acknowledge receipt.** Status → *Acknowledged*.

### Stage 9 — Return from production  (`demo_warehouse`)

The factory sends back 0.1 t unused from one bag. On the issue page, click **Receive return** on that line:

| Field | Type this | What it is / how it's used |
|---|---|---|
| Quantity | `0.1` | Cannot exceed what was issued minus earlier returns. |
| Returned by | `Suresh` | Who brought it back. |
| Send to lab for retest | ticked | Returned material becomes a **new bag**, blocked until the lab retests it. Unticked, it is held for an inspector instead. |
| Reason | `Excess after batch mix` | Required. |

Print the new bag's label (Unloading & put-away → *Labels to print*) and put it in `Q`.

### Stage 10 — Stock control  (`demo_warehouse`, then `demo_manager`)

**Returns, disposals & adjustments**

- **Adjustment** (a physical count found less in a bag): **Request adjustment** → package: one of your
  shelf bags → Type *Correction (physical count)* → Counted quantity `0.48` → Reference `COUNT-NOV-01`
  → Reason `Cycle count`. Nothing changes until a **different** person approves: sign in as
  `demo_manager` → **Approve**. The bag now shows 0.48 t and the ledger has an *Adjustment* movement.
- **Disposal / supplier return** of failed bag 4: **Record disposal** → package: bag 4 → Type
  *Return to supplier* → Quantity `0.5` → Reference `DN-901` (debit note) → Reason `Failed moisture test`.
  The bag leaves stock with status *Supplier returned*.
- **Ledger reconciliation** tab: should say *Ledger reconciled*.

### Stage 11 — Holds and recalls  (`demo_lab_reviewer`)

**Holds & recalls → Place hold / recall:** Type *Hold* → Scope *Material / lot* → Material
DEMO-MAT-001 → Lot `LOT-SC-2610` → Reason `Customer complaint check`.
Every bag of the lot is now blocked from issue. **Impact** lists what is still in stock and what was
already issued, and to which factory. **Release** the hold with notes. A *Recall* also covers bags already issued.

### Stage 12 — Oversight  (`demo_manager` / `demo_auditor`)

- **Management:** receipts by day and supplier, QC pass rate, gate → put-away time, lab turnaround, issues, exceptions (your FIFO override, adjustment, disposal).
- **Usage analysis:** people by role, peak people in one hour, workload per day.
- **Reports:** e.g. *Inward register*, *Lab register*, *Issue register* → **Export CSV / Excel**.
- **Audit trail:** every action above, with who, when and why. Click **Details** on an entry.
- **Search** (top bar): try `TEST-INV-1001`, `OD02AB4521`, `LOT-SC-2610`, `PO-TEST-2001` or a bag ID.

---

## Extra scenarios

| # | Scenario | Who / where | Steps and expected result |
|---|---|---|---|
| A | **Reject a whole line** | `demo_receiving`, truck DEMO-INV-C (2.4 t steel billets) | Start unloading → Verify → **Reject line**, received `2.4`, damaged `2.4`, reason `Rusted billets`. Line shows *Discrepant / Line rejected*; no bags can be created. As `demo_manager`, **Accept discrepancy** with a reason. Back as receiving: Submit verification → Mark unloaded. Nothing entered stock. |
| B | **Short delivery** | Any new receipt | Invoice 2 t, verify **received 1.9 t**, accept whole line, discrepancy notes `One bag short`. Line is flagged *Short −0.1*; receipt → *Exception*. `demo_manager` accepts it, then 1.9 t can be bagged. |
| C | **Weight discrepancy** | Gate | Gross `14250`, tare `12200` → net 2050 kg vs declared 2000 kg = 2.5 % > 0.5 %. The gate dashboard shows the receipt under *Discrepancy alerts*. |
| D | **Draft receipt** | `demo_gate`, DEMO-INV-D | Add a line, register, send for unloading: the same steps as Stage 1. |
| E | **Results for waiting samples** | `demo_lab_tech`, lot DEMO-LOT-B1 (HDPE) | Two samples already in testing: enter moisture `0.3` → submit → reviewer releases. |
| F | **Finish the demo request** | `demo_warehouse`, request DEMO-PO-1001 | Issue the remaining 0.5 t: the two reserved bags. Request becomes *Fulfilled*. |
| G | **Split / repack** | `demo_warehouse`, a shelf bag | Split / repack → quantities `0.2, 0.2`, reason `Repack into smaller bags` → two new labelled bags; the parent keeps the rest. |
| H | **Scan by camera** | Phone or tablet on the same network | Open the app over HTTPS, go to Scan → Camera → point at a printed QR code. The bag opens. |
| I | **Permissions** | Any user | Users only see their own menu; actions they may not do are refused by the server even if attempted. Example: the factory user cannot open supplier receipts. |
| J | **New user** | `demo_admin` → Users | Add user `test_gate` (role Gate operator); sign in as them. Deactivate them; sign-in then fails. |

---

## If something looks wrong

Every error message shows a **Reference** code. Note it together with the user, the screen and what you
typed; an administrator can find the exact request in the logs and in the audit trail.
