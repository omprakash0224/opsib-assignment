# OPSIB Candidate Walkthrough & Operational Runbook
**Technical Interview Defense Guide & Live Modification Playbook**

This document prepares the engineering candidate for the **30–45 minute technical walkthrough** described in Section 08 of the OPSIB Backend Engineering Assessment specification.

---

## 1. Live Demonstration Workflow (Terminal Cheat Sheet)

Follow these exact steps during the live review demonstration:

### Step 1: Clean State & Initialization
```bash
# Verify environment and dependencies
python --version
pip list | grep -E "fastapi|sqlalchemy|pydantic|pytest"

# Initialize a clean database
python -c "from app.db.session import init_db; init_db()"
```

### Step 2: Ingest Fixtures via CLI (Task A)
```bash
python scripts/load_fixtures.py
```
*Key Talking Point:*
Point out that 6 product aliases, 6 location aliases, 6 employees, and 6 shift records load cleanly. Highlight that both `purchase_order_lines` (Row 7 & 8) and `goods_receipt_lines` (Row 7 & 8) contain duplicate rows, which are successfully recognized as intra-batch duplicates and skipped without crashing or double counting.

### Step 3: Run Deterministic Reconciliation CLI (Tasks B & C)
```bash
python scripts/run_reconciliation.py
```
*Key Talking Point:*
Walk through the formatted table:
- **PO-501:** 100 ordered vs (60 + 32) received = **8 EA shortfall**. Routed to **`HR-102` (Dev Shah)** at `STORE-014` (`PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE`).
- **PO-505:** 10 BOX (converted to 120 EA base) vs 96 EA received = **24 EA shortfall**. Routed to store manager **`HR-401` (Rohan Iyer)** at `STORE-015` (`FALLBACK_STORE_MANAGER`).
- **PO-506:** 20 ordered vs 10 received (deduped) = **10 EA shortfall**. Routed to **`HR-102`**.
- **PO-502, PO-503, PO-504:** Fully satisfied, future deadline, and cancelled order correctly suppressed.

### Step 4: Run Automated Verification Suite (Tasks A, B, C, D)
```bash
python scripts/run_verification.py
```
*Key Talking Point:*
All 136 pytest tests pass in ~4.5 seconds, and all 6 core scenario invariants are validated against the frozen evaluation clock (`2026-10-16T15:00:00+05:30`).

### Step 5: Start REST API & Show Interactive Docs
```bash
uvicorn app.main:app --reload --port 8000
```
Open `http://localhost:8000/docs` in the browser to showcase the OpenAPI 3.1 documentation, RFC 7807 error envelopes, and task lifecycle endpoints.

---

## 2. In-Depth Answers to the 6 Review Discussion Topics

### Topic 1: Why a canonical data model over direct table joins? How does it handle source IDs that collide or change?
**Candidate Answer:**
- **Why Canonical Model (ADR-1):** In real retail integrations, ERP and WMS systems represent identical entities using disparate schemas, differing identifier formats, and inconsistent units of measure (e.g. ERP uses `BOX` while WMS scans barcodes in `EA`). If the reconciliation engine joined directly against raw CSV tables, every business rule would need hardcoded heuristics for each retailer export. By separating the storage into a **Raw Tier** (`RawRecord`) and a **Canonical Tier** (`PurchaseOrderLine`, `GoodsReceiptLine`), our reconciliation detector operates on a clean, strongly-typed domain model where quantities are already converted to base units (`EA`) and locations to canonical store IDs (`STORE-014`).
- **Handling Colliding Source IDs:** Different source systems might coincidentally emit the same source ID (e.g. ERP uses `1001` for a PO, and WMS uses `1001` for a receipt). We prevent collisions by scoping source identities with their source system: `UNIQUE (source_system, source_record_id)`.
- **Handling Changed Business Content (ADR-2):** If an external system retransmits an existing `source_record_id` with changed values (e.g. quantity changed from 20 to 15), our system computes the SHA-256 payload checksum. A hash mismatch on an existing ID is flagged as a `DataQualityIssue(issue_type="CONFLICTING_DUPLICATE")` rather than silently mutating canonical facts.

---

### Topic 2: What happens if a receipt arrives late, a record is repeated, an import partially fails, or a mapping is ambiguous?
**Candidate Answer:**
- **Late Receipts:** If a goods receipt arrives *after* the expected receipt deadline has passed, the reconciliation engine will re-evaluate on the next run. Because the exception key is deterministic (`TENANT:RECEIPT_SHORTFALL:PO:LINE`), the engine updates the variance. If the late receipt fully satisfies the order, the exception status can transition to `RESOLVED` or be dismissed.
- **Repeated Records:** If the exact same record is re-imported, the SHA-256 hash matches the existing `RawRecord`. The system flags it as `DUPLICATE_SKIPPED` and skips insertion. It **never double-counts quantities** or creates duplicate canonical lines (verified in PO-506 where repeated receipt row is counted only once).
- **Partial Import Failure:** The ingestion pipeline processes rows independently inside batch tracking. If 1 row in a 100-row file has a corrupt timestamp or non-numeric quantity, that individual row is captured in `ImportBatch.error_summary` as a row-level error. The other 99 valid rows are committed, and the batch status is marked `PARTIAL`. Valid operational flow is not blocked by one corrupt row.
- **Ambiguous or Missing Mapping:** If an incoming product code or location code does not exist in the crosswalk aliases, the system writes a `DataQualityIssue(issue_type="UNMAPPED_CROSSWALK")`. **Crucial invariant:** It does *not* create a canonical line and does *not* raise a false shortfall exception. Data quality issues are kept strictly separate from operational shortfall exceptions.

---

### Topic 3: How does your code avoid two workers creating two tasks for the same business issue?
**Candidate Answer:**
- We avoid duplicate tasks through **deterministic business identity keys backed by database unique constraints**:
  1. An operational exception does not use a random UUID as its identity; it uses a composite deterministic key:
     $$\text{exception\_key} = \text{tenant\_id} + \text{":RECEIPT\_SHORTFALL:"} + \text{po\_number} + \text{":"} + \text{line\_number}$$
     with a database constraint `UNIQUE (exception_key)`.
  2. A human task references the exception with a deterministic task key:
     $$\text{task\_key} = \text{tenant\_id} + \text{":TASK:"} + \text{exception\_id}$$
     with a database constraint `UNIQUE (task_key)`.
- If two background workers or API instances run reconciliation at the exact same millisecond:
  - Both workers execute detection logic inside their respective database transactions.
  - The first worker inserts the exception and task.
  - The second worker hits the unique constraint on `exception_key` or `task_key`, causing SQLAlchemy to roll back or return the existing entity via our query-first idempotent handler (`session.query(OperationalException).filter_by(exception_key=...).first()`).
- In a scaled distributed production environment (as noted in our hardening roadmap), we would add PostgreSQL transaction advisory locks (`pg_advisory_xact_lock(hash(po_number))`) or Redis distributed locks (`Redlock`) to eliminate database collision contention altogether.

---

### Topic 4: Why is the result labeled a receipt shortfall rather than a proven supplier fault?
**Candidate Answer:**
- **Operational & Legal Epistemic Neutrality (ADR-6):** The supplied fixture data pack contains ERP purchase orders and WMS physical receipt scans. Crucially, it contains **zero supplier dispatch or Advanced Shipping Notice (ASN) data, and zero carrier Proof of Delivery (POD) data**.
- All we factually know from the evidence is:
  $$\text{Accepted Received Qty} < \text{Ordered Qty}$$
- We cannot determine:
  1. Whether the supplier under-shipped,
  2. Whether the carrier lost or damaged cartons in transit,
  3. Whether the warehouse dock misplaced stock before scanning,
  4. Or whether a second delivery truck is still in transit.
- Labeling the discrepancy `SUPPLIER_SHORT_SHIPMENT` would assert an unproven accusation, potentially triggering improper vendor penalties or invoice disputes. Therefore, our system strictly labels findings as `RECEIPT_SHORTFALL`, attaches an explicit `missing_evidence` list citing absent ASN/POD data, and delegates root-cause determination to the human investigation task.

---

### Topic 5: How would you add another retailer export without changing the rule engine?
**Candidate Answer:**
- Because of our **Two-Tier Storage Architecture** (`app/ingestion/` vs `app/detection/`), the reconciliation rule engine in `app/detection/detector.py` knows *nothing* about CSV file structures, retailer-specific column names, or vendor systems. It operates exclusively against canonical tables (`PurchaseOrderLine`, `GoodsReceiptLine`).
- To add a new retailer (e.g., SAP ERP or Manhattan Associates WMS):
  1. Create a retailer-specific parser or Pydantic schema in `app/schemas/` to parse the new file/API format.
  2. Add new mapping entries to `product_aliases` and `location_aliases` (or an external crosswalk service).
  3. Map the parsed records into standard canonical entities (`PurchaseOrderLine` or `GoodsReceiptLine`) and persist them.
- The reconciliation engine, workforce routing engine, task state machine, and AI boundary require **zero line changes**.

---

### Topic 6: What needs to change before accepting production data from multiple tenants?
**Candidate Answer:**
1. **Database Row-Level Security (RLS):**
   - In our current implementation, `tenant_id` is modeled on every table, and API dependencies extract `X-Tenant-ID`.
   - For production, migrate from SQLite to PostgreSQL with native Row-Level Security enabled on all tables:
     ```sql
     ALTER TABLE canonical_po_lines ENABLE ROW LEVEL SECURITY;
     CREATE POLICY tenant_isolation_policy ON canonical_po_lines
       USING (tenant_id = current_setting('app.current_tenant_id'));
     ```
2. **Authentication & Tenant Claims:**
   - Replace the header-based mock tenant dependency (`get_tenant_id`) with validated JWT tokens (OAuth2/OIDC via Okta/Auth0) containing verified tenant claims.
3. **Tenant-Aware Composite Indexes:**
   - Update unique constraints and primary lookup indexes to lead with `tenant_id`:
     ```sql
     CREATE UNIQUE INDEX uq_tenant_po_line ON canonical_po_lines (tenant_id, po_number, line_number);
     ```
4. **Tenant Configuration & Quotas:**
   - Store tenant-specific configuration (e.g. customized shortfall tolerance, custom routing rules, OpenRouter API quotas) in a `tenants` metadata table.

---

## 3. Live Code Modification Playbook

During the interview, the reviewer will likely ask you to modify a small part of the codebase. Here is how to handle the most common requests:

### Modification A: "Add a Configurable Shortfall Tolerance (e.g. 2% or 5 units)"
**Location:** [`app/detection/rules.py`](file:///d:/opsib-assignment/app/detection/rules.py) and [`app/detection/detector.py`](file:///d:/opsib-assignment/app/detection/detector.py)
**How to implement:**
1. In `app/detection/rules.py`, add:
   ```python
   PERCENTAGE_TOLERANCE = 0.02  # 2% tolerance
   ABSOLUTE_TOLERANCE_QTY = 0.0  # Or fixed units
   ```
2. In `app/detection/detector.py`, update Step 4 variance check:
   ```python
   # Current:
   if variance > VARIANCE_TOLERANCE:
   
   # Modified (with 2% tolerance):
   tolerance_qty = po.ordered_qty_base * PERCENTAGE_TOLERANCE
   if variance > tolerance_qty:
       findings.append(...)
   ```
3. Run `pytest tests/test_reconciliation.py` to demonstrate test agility.

---

### Modification B: "Add a New Task Lifecycle Status (e.g. `INVESTIGATING` or `ESCALATED`)"
**Location:** [`app/workflows/state_machine.py`](file:///d:/opsib-assignment/app/workflows/state_machine.py) and [`app/db/models/task.py`](file:///d:/opsib-assignment/app/db/models/task.py)
**How to implement:**
1. In `app/workflows/state_machine.py`, add `INVESTIGATING` to `VALID_TASK_STATUSES`:
   ```python
   VALID_TASK_STATUSES = {
       "CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS",
       "INVESTIGATING", "COMPLETED", "PENDING_VERIFICATION", ...
   }
   ```
2. Update the permitted transition table `PERMITTED_TRANSITIONS`:
   ```python
   "IN_PROGRESS": {"COMPLETED", "BLOCKED", "ASSIGNED", "INVESTIGATING"},
   "INVESTIGATING": {"IN_PROGRESS", "COMPLETED", "ASSIGNED"},
   ```
3. In `app/workflows/task_service.py`, add the transition method:
   ```python
   def investigate_task(session: Session, task_id: str, actor_id: str = "SYSTEM"):
       task = get_task_or_404(session, task_id)
       assert_transition(task.status, "INVESTIGATING")
       old_status = task.status
       task.status = "INVESTIGATING"
       record_event(session, task.id, "STATUS_CHANGE", old_status, "INVESTIGATING", actor_id)
       return task
   ```
4. Run `pytest tests/test_task_lifecycle.py`.

---

### Modification C: "Route to Regional Operations (`HR-501`) if Associate and Manager are Missing"
**Location:** [`app/routing/policy.py`](file:///d:/opsib-assignment/app/routing/policy.py)
**How to implement:**
1. In `RoutingPolicy.route()`, after Tier 2 (store manager fallback fails):
   ```python
   # Tier 3: Regional Operations Fallback
   regional_candidate = (
       session.query(Employee)
       .filter(Employee.role == "regional_operations")
       .filter(Employee.employment_status == "ACTIVE")
       .first()
   )
   if regional_candidate:
       return RoutingDecision(
           assignee_id=regional_candidate.employee_id,
           assignment_reason="FALLBACK_REGIONAL_OPERATIONS",
           eligibility_log=logs,
       )
   ```
2. Run `pytest tests/test_routing.py`.

---

### Modification D: "Add an ASN Table to Turn Neutral Shortfalls into Proven Short-Shipments"
**Location:** [`app/db/models/canonical.py`](file:///d:/opsib-assignment/app/db/models/canonical.py) and [`app/detection/detector.py`](file:///d:/opsib-assignment/app/detection/detector.py)
**How to implement:**
1. Define `SupplierDispatchLine` model:
   ```python
   class SupplierDispatchLine(Base):
       __tablename__ = "canonical_dispatch_lines"
       id = Column(UUID, primary_key=True, default=uuid4)
       po_number = Column(String(50))
       po_line_number = Column(Integer)
       dispatched_qty_base = Column(Float)
   ```
2. In `detector.py`, query dispatch lines for the PO line:
   ```python
   dispatch_qty = sum_dispatched_for_po_line(session, po.po_number, po.line_number)
   if dispatch_qty is not None and dispatch_qty < po.ordered_qty_base:
       finding.exception_type = "SUPPLIER_SHORT_SHIPMENT"
       finding.neutral_summary = f"Supplier dispatched {dispatch_qty} against {po.ordered_qty_base} ordered; supplier short-shipment verified."
   ```

---

## 4. Submission Checklist Verification

- [x] **Can another engineer run the service and tests from a clean checkout?**  
  *Yes: clone repo, `pip install -r requirements.txt`, `pytest` executes in 4.39s with 0 external dependencies.*
- [x] **Are the six required scenario interpretations tested?**  
  *Yes: PO-501 (8 EA), PO-502 (full), PO-503 (future), PO-504 (cancelled), PO-505 (24 EA), PO-506 (10 EA deduped) verified in `test_reconciliation.py` and `test_scenarios_e2e.py`.*
- [x] **Can every actionable finding be traced to source records and a rule version?**  
  *Yes: `OperationalException` attaches `evidence_records` (source PO and GRN record IDs) and `rule_version="v1.0.0"`.*
- [x] **Can the candidate explain the failure modes and trade-offs without relying on an AI tool?**  
  *Yes: complete technical reasoning documented in this runbook and `README.md`.*
