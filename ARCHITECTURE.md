# OPSIB Backend Architecture Specification
**Retail Data Integration, Exception Detection & Task Reliability**

---

## 1. System Overview & Purpose

OPSIB is an operational intelligence backend designed to bridge disparate retail systems (ERP, WMS, HRMS, and Workforce Management). Its primary purpose is to:
1. Ingest messy, siloed datasets (with disparate product/location identifiers, conflicting units of measure, and duplicate transmissions).
2. Normalize and reconcile records into a canonical representation while preserving raw provenance.
3. Deterministically detect operational exceptions—specifically goods receipt shortfalls against purchase orders—without making unverified root-cause accusations.
4. Route actionable tasks to eligible, on-shift personnel using organizational fallback rules.
5. Manage an auditable task lifecycle with server-enforced state transitions and idempotent retries.
6. Provide an optional, tightly-sandboxed AI investigation advisory boundary with citation verification.

---

## 2. System Architecture Diagram

```mermaid
graph TB
    subgraph "External Systems / Data Sources"
        ERP["ERP System<br/>(PO Lines, SKUs, Stores)"]
        WMS["WMS Warehouse<br/>(Goods Receipts, Barcodes)"]
        HRMS["HRMS Roster<br/>(Employees, Roles, Orgs)"]
        WFM["Workforce Management<br/>(Shift Availability)"]
    end

    subgraph "Ingestion & Normalization Layer (Task A)"
        API_INGEST["Ingestion API & CLI<br/>(POST /imports/{dataset})"]
        VAL["Typed Schema Validator<br/>(Pydantic v2)"]
        DEDUP["Deduplication & Hash Engine<br/>(SHA-256 Content Hash)"]
        XWALK["Crosswalk Resolver<br/>(Product & Location Aliases, UOM)"]
        DQ_STORE["Data Quality Store<br/>(Unmapped SKUs, Conflicts)"]
    end

    subgraph "Persistence Layer (SQLAlchemy 2.0 / PostgreSQL / SQLite)"
        RAW_DB[("Raw Record Store<br/>(Immutable Raw Payloads & Provenance)")]
        CANON_DB[("Canonical Domain Store<br/>(Normalized POs, Receipts, Roster)")]
        EXC_DB[("Operational Exception Store<br/>(Shortfalls & Evidence)")]
        TASK_DB[("Task & Workflow Store<br/>(Tasks, Assignments, Audits)")]
    end

    subgraph "Deterministic Rule Engine (Task B)"
        CLOCK["Frozen Time Provider<br/>(2026-10-16T15:00:00+05:30)"]
        RECON["Deterministic Reconciliation Engine<br/>(Receipt Shortfall Detector v1.0.0)"]
    end

    subgraph "Routing & Lifecycle Engine (Task C)"
        ROUTING["Workforce Policy Engine<br/>(Eligibility, Shift Freshness, Fallback)"]
        STATE_MACH["Task State Machine<br/>(Strict Transitions & Retries)"]
        AUDIT["Immutable Audit Trail<br/>(State Changes & Assignment Events)"]
    end

    subgraph "Advisory Copilot Boundary (Task D)"
        AI_GATEWAY["Claude AI Investigation Provider<br/>(Advisory Only, No State Changes)"]
        GUARD["Citation Guardrail Validator<br/>(Evidence Citation Integrity Check)"]
    end

    subgraph "API & Client Surface"
        REST_API["FastAPI REST Endpoints<br/>(/imports, /exceptions, /tasks)"]
        CLIENT["Store Associates, Managers & Ops Reviewers"]
    end

    ERP --> API_INGEST
    WMS --> API_INGEST
    HRMS --> API_INGEST
    WFM --> API_INGEST

    API_INGEST --> VAL
    VAL --> DEDUP
    DEDUP --> RAW_DB
    DEDUP -->|Missing Mapping / Conflict| DQ_STORE
    DEDUP --> XWALK
    XWALK --> CANON_DB

    CLOCK --> RECON
    CANON_DB --> RECON
    RECON --> EXC_DB

    EXC_DB --> ROUTING
    CANON_DB --> ROUTING
    ROUTING --> TASK_DB
    STATE_MACH --> TASK_DB
    STATE_MACH --> AUDIT

    EXC_DB --> AI_GATEWAY
    AI_GATEWAY --> GUARD
    GUARD --> REST_API

    REST_API --> CLIENT
    CLIENT --> REST_API
```

---

## 3. Detailed Data Flows & Pipelines

### 3.1 Ingestion, Deduplication & Data Quality Pipeline (Task A)

```mermaid
flowchart TD
    A["Raw CSV / JSON Row Arrival"] --> B["Validate Structure & Types with Pydantic"]
    B -->|Schema Violation| B_ERR["Write to Import Errors / Reject Row"]
    B -->|Valid Structure| C["Compute Payload Hash (SHA-256)"]
    C --> D{"Lookup Source Record ID"}
    
    D -->|Found with Identical Hash| E["Idempotent Repeat: Mark DUPLICATE_SKIPPED (No-op)"]
    D -->|Found with Conflicting Hash| F["Log CONFLICTING_DUPLICATE in Data Quality Store (Flag Batch)"]
    D -->|Not Found - New Record| G["Persist to RawRecord Store (Provenance Preserved)"]
    
    G --> H{"Resolve Crosswalk Aliases"}
    H -->|Product or Location Unmapped| I["Log UNMAPPED_ITEM in Data Quality Store"]
    H -->|Valid Crosswalk Mapping| J["Normalize Units (e.g., 10 BOX * 12 -> 120 EA)"]
    J --> K["Persist Canonical Record (PO Line / Receipt Line / Employee)"]
```

---

### 3.2 Deterministic Reconciliation & Shortfall Detector (Task B)

```mermaid
flowchart TD
    START["Trigger Reconciliation (Frozen Clock: 2026-10-16T15:00:00+05:30)"] --> FETCH["Fetch Eligible PO Lines from Canonical Store"]
    
    FETCH --> CHECK_CANCEL{"Is PO Line Status CANCELLED?<br/>e.g., PO-504"}
    CHECK_CANCEL -->|Yes| SKIP_CANCEL["Skip: Cancelled order requires no receipt"]
    CHECK_CANCEL -->|No| CHECK_DEADLINE{"Is expected_receipt_by <= FROZEN_TIME?<br/>e.g., PO-503 window open"}
    
    CHECK_DEADLINE -->|No - Future Deadline| SKIP_FUTURE["Skip: Receipt window still open; not actionable"]
    CHECK_DEADLINE -->|Yes - Deadline Expired| AGG_RECEIPTS["Aggregate POSTED Goods Receipts for PO Line"]
    
    AGG_RECEIPTS --> DEDUP_CHECK["Ensure Unique GoodsReceiptLines (Duplicate-safe)"]
    DEDUP_CHECK --> SUM_QTY["Calculate Total Accepted Quantity in Base UOM"]
    SUM_QTY --> CALC_VAR["Compute Variance = Ordered Base Qty - Accepted Base Qty"]
    
    CALC_VAR --> CHECK_VAR{"Variance > 0 Tolerance?"}
    CHECK_VAR -->|Variance <= 0| NO_EXC["No Shortfall: Order fully satisfied (e.g., PO-502)"]
    CHECK_VAR -->|Variance > 0| CREATE_EXC["Generate OperationalException Record"]
    
    CREATE_EXC --> GEN_KEY["Construct Deterministic Key: TENANT:SHORTFALL:PO_NUM:LINE"]
    GEN_KEY --> ATTACH_EVIDENCE["Attach Provenance: Linked Source IDs & Rule v1.0.0"]
    ATTACH_EVIDENCE --> NEUTRAL_NOTE["Attach Neutral Summary (Acknowledge missing ASN data)"]
    NEUTRAL_NOTE --> SAVE_EXC["Persist OperationalException in DB"]
```

---

### 3.3 Workforce Routing & Fallback Decision Flow (Task C)

```mermaid
flowchart TD
    EXC_IN["OperationalException Raised at Location<br/>(e.g., STORE-014 or STORE-015)"] --> Q_RECEIVER["Query Employees at Location with Role = receiving_associate"]
    
    Q_RECEIVER --> FILTER_HR{"Filter 1: Is employment_status == ACTIVE?"}
    FILTER_HR -->|No - Inactive Status| REJECT_INACTIVE["Reject: Inactive employment overrides shift<br/>(e.g., HR-103)"]
    FILTER_HR -->|Yes - Active Status| FILTER_SHIFT{"Filter 2: Is availability_status == ON_SHIFT?<br/>Fresh at frozen time"}
    
    FILTER_SHIFT -->|No - Off Shift| REJECT_OFF["Reject: Associate currently off shift<br/>(e.g., HR-101)"]
    FILTER_SHIFT -->|Yes - On Shift| SELECT_PRIMARY["Assign to Primary Receiver Dev Shah (HR-102)<br/>Reason: PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE"]
    
    Q_RECEIVER -->|No Candidate at Location| FALLBACK_MGR["Execute Fallback: Query Active, On-Shift store_manager"]
    FALLBACK_MGR --> CHECK_MGR{"Manager Found & Eligible?"}
    CHECK_MGR -->|Yes - Manager Eligible| SELECT_FALLBACK["Assign to Store Manager Rohan Iyer (HR-401)<br/>Reason: FALLBACK_STORE_MANAGER"]
    CHECK_MGR -->|No - No Eligible Fallback| UNASSIGNED_QUEUE["Queue Task as UNASSIGNED<br/>Reason: MANUAL_ASSIGNMENT_REQUIRED"]
    
    SELECT_PRIMARY --> CREATE_TASK["Create HumanTask & Write TaskAuditEvent(ASSIGNED)"]
    SELECT_FALLBACK --> CREATE_TASK
    UNASSIGNED_QUEUE --> CREATE_TASK
```

---

### 3.4 Task State Machine & Verification Lifecycle (Task C)

```mermaid
stateDiagram-v2
    [*] --> CREATED: Task Initialized
    CREATED --> ASSIGNED: Eligible Associate/Manager Selected
    CREATED --> UNASSIGNED: No Eligible Candidate Found
    UNASSIGNED --> ASSIGNED: Manual Manager Assignment
    
    ASSIGNED --> ACKNOWLEDGED: Employee Acknowledges Task
    ACKNOWLEDGED --> IN_PROGRESS: Investigation Commenced
    
    ASSIGNED --> REASSIGNED: Reassign with Reason
    ACKNOWLEDGED --> REASSIGNED: Reassign with Reason
    IN_PROGRESS --> REASSIGNED: Reassign with Reason
    REASSIGNED --> ASSIGNED: New Assignee Set
    
    IN_PROGRESS --> BLOCKED: Awaiting External Supplier Feedback
    BLOCKED --> IN_PROGRESS: Supplier Feedback Received
    
    IN_PROGRESS --> COMPLETED: Submit Resolution Code & Evidence Ref
    COMPLETED --> PENDING_VERIFICATION: Awaiting Quality Check
    PENDING_VERIFICATION --> VERIFIED: Verification Criteria Met -> Exception Closed
    
    PENDING_VERIFICATION --> REOPENED: Evidence Inadequate
    REOPENED --> IN_PROGRESS: Rework Required
    
    VERIFIED --> [*]
```

---

### 3.5 Claude AI Advisory Boundary & Guardrail Architecture (Task D)

```mermaid
flowchart TD
    EXC["Operational Exception Record"] --> BUNDLE["Package Grounded Evidence Context<br/>(PO Qty, Received Lines, Supplier, UOM, Notes)"]
    BUNDLE --> CALL_AI["Invoke Claude Investigation Provider<br/>(Temperature=0.1, Structured JSON Prompt)"]
    
    CALL_AI --> RESP["Receive Raw Structured JSON Response"]
    RESP --> SCHEMA_VAL{"Schema Validation<br/>(InvestigationBrief Pydantic Schema)"}
    
    SCHEMA_VAL -->|Invalid Schema| FALLBACK_ERR["Reject: Fallback to Raw Exception Data"]
    SCHEMA_VAL -->|Valid Schema| GUARDRAIL{"Citation Guardrail Validator"}
    
    GUARDRAIL --> CHECK_IDS{"Are all cited_record_ids present in supplied evidence package?"}
    CHECK_IDS -->|Hallucinated ID Detected| STRIP_ID["Strip Invalid Citations & Log Safety Warning"]
    CHECK_IDS -->|All IDs Validated| SAFE_BRIEF["Grounded Investigation Brief Available to Reviewer"]
    STRIP_ID --> SAFE_BRIEF
    
    subgraph "Strict Boundary Rules"
        RULE1["No Authority: Cannot compute quantities"]
        RULE2["No Persistence: Cannot modify DB state"]
        RULE3["No External Writes: Cannot call ERP/WMS APIs"]
    end
```

---

## 4. Entity-Relationship Architecture

```mermaid
erDiagram
    ImportBatch ||--o{ RawRecord : contains
    ImportBatch {
        uuid id PK
        string dataset_name
        string status
        int total_rows
        int accepted_rows
        int duplicate_rows
        int rejected_rows
        datetime created_at
    }

    RawRecord {
        uuid id PK
        uuid batch_id FK
        string dataset_name
        string source_record_id
        string content_hash
        string status
        json payload
    }

    ProductAlias {
        string source_system PK
        string external_item_id PK
        string canonical_product_id
        string source_uom
        string base_uom
        numeric to_base_factor
    }

    LocationAlias {
        string source_system PK
        string external_location_id PK
        string canonical_location_id
        string region_id
    }

    PurchaseOrderLine {
        uuid id PK
        string source_system
        string source_record_id UK
        string po_number
        int line_number
        string supplier_code
        string canonical_location_id
        string canonical_product_id
        numeric ordered_qty_base
        string base_uom
        string status
        datetime expected_receipt_by
    }

    GoodsReceiptLine {
        uuid id PK
        string source_system
        string source_record_id UK
        string receipt_number
        int receipt_line
        string po_number
        int po_line_number
        string canonical_location_id
        string canonical_product_id
        numeric accepted_qty_base
        string base_uom
        datetime received_at
        string posting_status
    }

    Employee ||--o{ HumanTask : assigned_to
    Employee {
        string employee_id PK
        string display_name
        string employment_status
        string role
        string canonical_location_id
        string manager_employee_id
        string region_id
    }

    WorkforceShift {
        string employee_id PK
        string availability_status
        datetime shift_start
        datetime shift_end
        datetime observed_at
    }

    OperationalException ||--o{ HumanTask : requires
    OperationalException {
        uuid id PK
        string exception_key UK
        string exception_type
        string status
        string po_number
        int po_line_number
        string canonical_product_id
        string canonical_location_id
        numeric expected_qty_base
        numeric received_qty_base
        numeric variance_qty_base
        string base_uom
        string rule_version
        json evidence_records
        string neutral_summary
        string missing_evidence
        datetime detected_at
    }

    HumanTask ||--o{ TaskAuditEvent : audits
    HumanTask {
        uuid id PK
        uuid exception_id FK
        string task_key UK
        string status
        string assignee_id FK
        string assignment_reason
        string canonical_location_id
        string resolution_code
        string resolution_evidence_ref
        datetime created_at
        datetime updated_at
    }

    TaskAuditEvent {
        uuid id PK
        uuid task_id FK
        string actor_id
        string event_type
        string from_status
        string to_status
        string reason
        json event_payload
        datetime timestamp
    }

    DataQualityIssue {
        uuid id PK
        string source_system
        string dataset_name
        string source_record_id
        string issue_type
        json details
        datetime created_at
    }
```

---

## 5. Architectural Decision Records (ADRs)

### ADR-1: Two-Tier Storage Architecture (Raw Provenance vs Canonical Domain Model)
- **Status:** Accepted
- **Context:** Source ERP and WMS systems supply heterogeneous identifiers, different column names, and varying units of measure. Reconciling directly against raw CSV structures makes detection rules fragile and tightly coupled to current source formats.
- **Decision:** Implement a two-tier storage architecture:
  1. **Raw Tier (`RawRecord`, `ImportBatch`):** Stores unmutated raw payloads, content hashes (SHA-256), source system name, and batch identifiers. This guarantees complete lineage and non-repudiation.
  2. **Canonical Tier (`PurchaseOrderLine`, `GoodsReceiptLine`, `Employee`):** Clean, strongly-typed entities with normalized product identifiers (`SKU-001`), canonical locations (`STORE-014`), and standardized base units (`EA`).
- **Why:** Separating raw ingestion from canonical state allows future retailer formats to be integrated simply by adding a mapping adapter, without changing the reconciliation rules.
- **Trade-offs:** Additional database write per record and slight schema overhead, but delivers audit traceability and decoupling.

---

### ADR-2: Idempotency & Conflict Policy (Duplicate Detection vs Business Change)
- **Status:** Accepted
- **Context:** CSV exports from retail backends often repeat rows across daily batches. Occasionally, a previously received source ID is retransmitted with altered values (e.g. quantity modified from 20 to 15).
- **Decision:**
  - Enforce a database unique constraint on `(source_system, source_record_id)`.
  - On arrival of an existing `source_record_id`:
    - Compute `SHA-256` content hash of incoming payload.
    - If hash matches existing record $\rightarrow$ **Idempotent Duplicate**: Return existing record status, log `DUPLICATE_SKIPPED`, do not double-count quantities.
    - If hash differs $\rightarrow$ **Conflicting Update**: Do *not* silently overwrite existing business state. Create a `DataQualityIssue` record (`CONFLICTING_DUPLICATE`), mark row for human operational review, and preserve both versions.
- **Why:** Prevents stock distortion (e.g., PO-506 where GRN was duplicated) while preventing unauthorized mutations of already posted operational facts.
- **Trade-offs:** In-place updates require explicit data-correction workflows rather than automatic silent overwrites.

---

### ADR-3: Temporal Isolation & Frozen Clock Execution
- **Status:** Accepted
- **Context:** Assessment scenario logic relies on exact evaluation timing (`2026-10-16T15:00:00+05:30`). For example, PO-503 has an expected receipt deadline of `2026-10-17T18:00:00+05:30`. If the detector queries the local system clock (`datetime.now()`), PO-503 would prematurely trigger a false shortfall exception.
- **Decision:**
  - Introduce a central `ClockProvider` / `get_current_time()` configured to the frozen timestamp `2026-10-16T15:00:00+05:30`.
  - All deadline boundary checks and shift availability evaluations inject this provider.
- **Why:** Guarantees deterministic, reproducible test results and isolates business logic from server execution environments.
- **Trade-offs:** In production, the clock provider reads UTC system time; in test and historical backfill modes, it reads the target evaluation window.

---

### ADR-4: Decoupling Operational Exceptions from Human Tasks
- **Status:** Accepted
- **Context:** An operational anomaly (receipt shortfall) is a business finding. A task is actionable human labor. Conflating the two creates major bugs: reassigning a task should not delete or re-detect the exception; closing an exception may require multiple coordinated tasks.
- **Decision:**
  - Create separate database entities: `OperationalException` and `HumanTask`.
  - `OperationalException` is keyed deterministically by business identity (`TENANT:RECEIPT_SHORTFALL:PO-501:L1`).
  - `HumanTask` references `exception_id` via foreign key and maintains its own state lifecycle, assignee, and resolution history.
- **Why:** Multiple tasks (e.g., dock physical search + supplier invoice adjustment) can point to the same exception. If a task is reassigned or reopened, the exception finding remains stable and unchanged.
- **Trade-offs:** Requires an additional join when querying tasks with exception details, but properly isolates operational state from human workflow.

---

### ADR-5: Server-Enforced State Machine & Idempotent Client Retry Handling
- **Status:** Accepted
- **Context:** If a client invokes `POST /tasks/{id}/complete` and the connection times out after the database commits, the client will retry the request. A naive server would return `409 Conflict` (cannot complete an already completed task), confusing the client.
- **Decision:**
  - Implement a server-enforced state machine rejecting illegal arbitrary transitions.
  - Support `Idempotency-Key` headers on mutating endpoints.
  - If a task transition request is repeated with the same intent and payload on an already transitioned task, return the existing status with `200 OK` rather than failing.
- **Why:** Eliminates duplicate work, avoids race conditions, and ensures network retry resilience.
- **Trade-offs:** Requires caching or querying previous task transition payloads.

---

### ADR-6: Epistemic Neutrality (Preserving Unknowns & Missing Evidence)
- **Status:** Accepted
- **Context:** Retail systems often jump to blaming suppliers when goods receipts are less than purchase orders. However, the available data lacks Advanced Shipping Notices (ASN) and carrier proof of delivery.
- **Decision:**
  - The detector labels findings as `RECEIPT_SHORTFALL`, never `SUPPLIER_SHORT_SHIPMENT`.
  - Findings explicitly carry metadata: `missing_evidence: ["SUPPLIER_DISPATCH_ASN", "CARRIER_POD"]`.
  - A neutral summary states the arithmetic discrepancy without inferring blame or financial liability.
- **Why:** Operational integrity. Prevents incorrect financial clawbacks and protects retailer credibility.
- **Trade-offs:** Root-cause determination is deferred to investigation rather than being automated on partial evidence.

---

### ADR-7: Claude AI Advisory Boundary with Grounded Citation Guardrails
- **Status:** Accepted
- **Context:** Large Language Models can hallucinate IDs, make arithmetic errors, and produce non-deterministic outputs if given uncontrolled operational autonomy.
- **Decision:**
  - AI is strictly restricted to an **advisory copilot role**.
  - Claude never calculates quantities, evaluates deadlines, assigns employees, or updates database records.
  - An output guardrail validator checks every cited record ID against the evidence package. Any ungrounded or hallucinated reference is stripped and flagged.
  - A typed interface (`ClaudeInvestigationProvider`) and deterministic offline mock provider are included so tests execute without external API keys or costs.
- **Why:** Guarantees enterprise safety, prevents hallucinated audits, and maintains zero external dependency for tests.
- **Trade-offs:** AI cannot execute automated resolutions, but remains 100% auditable and safe.

---

### ADR-8: Database Persistence Strategy (PostgreSQL / SQLite Dual Compatibility)
- **Status:** Accepted
- **Context:** The production target is PostgreSQL, but assessment reviewers need to execute tests immediately from a clean checkout without spinning up external database containers.
- **Decision:**
  - Use SQLAlchemy 2.0 with standard ANSI SQL datatypes and SQLite-compatible JSON handling.
  - Use SQLite in WAL (Write-Ahead Logging) mode with foreign keys enabled (`PRAGMA foreign_keys = ON`) by default for tests and single-command local runs.
  - Provide complete database connection configuration supporting PostgreSQL via environment variable `DATABASE_URL`.
- **Why:** Guarantees zero friction for review execution while maintaining full migration readiness for PostgreSQL.
- **Trade-offs:** Cannot use Postgres-specific extensions (e.g. pg_trgm) in core models, but standard SQLAlchemy models are clean and portable.

---

## 6. Trade-offs, Known Limitations & Production Hardening

### 6.1 Intentional Simplifications in Current Scope

| Area | Current Implementation | Rationale for Assessment Scope |
| :--- | :--- | :--- |
| **Worker Infrastructure** | Synchronous REST pipeline (`POST /reconciliation/run`) | Adding Celery/Kafka introduces unnecessary infrastructure complexity for a 6-scenario bounded dataset. |
| **Authentication** | Header-based mock tenant/user context (`X-Tenant-ID`, `X-User-ID`) | Full OAuth2/OIDC adds boilerplate without testing operational reconciliation logic. |
| **Database** | SQLite WAL mode by default; PostgreSQL ready | Enables instant `pytest` execution on any machine without Docker or local PostgreSQL daemon. |
| **AI Integration** | Mock provider + typed Claude API contract | Prevents financial cost or external network failures during automated test runs. |

---

### 6.2 Production Hardening Roadmap (What Changes for Live Deployment)

1. **Multi-Tenancy & Data Isolation:**
   - Add row-level security (RLS) in PostgreSQL based on `tenant_id`.
   - Ensure all queries, indexes, and unique constraints include `tenant_id` as the leading composite key column.
2. **Distributed Locks & Concurrency:**
   - Introduce Redis-backed distributed locks (`Redlock`) or Postgres advisory locks (`pg_advisory_xact_lock`) on `(tenant_id, po_number)` during reconciliation to prevent dual-worker race conditions.
3. **Secrets & Integration Credentials:**
   - Store ERP/WMS API tokens, database credentials, and Claude API keys in AWS Secrets Manager or HashiCorp Vault, injecting them into the application environment at runtime.
4. **Asynchronous Ingestion & Streaming:**
   - Transition file uploads to S3 pre-signed URLs with SQS/Kafka event triggers, processing large millions-of-rows files via streaming chunked workers.
5. **Audit Retention & Non-Repudiation:**
   - Stream `TaskAuditEvent` records to an append-only, immutable storage layer (e.g., AWS S3 Object Lock / ClickHouse) for regulatory compliance.

---

## 7. Answers to Assessment Design Questions

### Question 1: Preventing Duplicate Business Effects on Concurrent / Retried Imports
> *How do you prevent a second import or concurrent worker from creating a different result from the first import? Explain the database constraint or transaction boundary that makes your answer true.*

**Implementation Answer:**
1. **Source Level Idempotency:** A composite unique constraint `UNIQUE (source_system, source_record_id)` exists on the `canonical_po_line` and `canonical_receipt_line` tables.
2. **Transaction Boundary:** Ingestion runs inside a database transaction (`BEGIN ... COMMIT`). Before inserting, incoming records are checked against existing rows:
   - If a row exists with an identical `SHA-256` content hash, the row is flagged as `DUPLICATE_SKIPPED` and no insert occurs.
   - If a row exists with differing content, a `DataQualityIssue` is created rather than mutating existing data.
3. **Deterministic Exception Keys:** Operational exceptions use a deterministic business key:
   $$\text{exception\_key} = \text{MD5}(\text{tenant\_id} + \text{"RECEIPT_SHORTFALL"} + \text{po\_number} + \text{line\_number})$$
   A database unique constraint `UNIQUE (exception_key)` guarantees that even if reconciliation runs concurrently on two separate workers, the second worker hits an `ON CONFLICT DO NOTHING` or duplicate key boundary, preventing duplicate exceptions or duplicate tasks.

---

### Question 2: Client Timeout During Task Completion & Retry Safety
> *If the API commits task completion but the client times out before receiving the response, what happens when the client retries? Describe and implement a safe answer.*

**Implementation Answer:**
1. **Idempotency Key & Transition Guard:** When completing a task, the client submits:
   `POST /api/v1/tasks/{id}/complete` with payload `{"resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED", "evidence_ref": "REF-8842"}`.
2. **Safe Idempotent Response:**
   - The task transition handler checks the current database state of the task.
   - If `current_status == 'COMPLETED'`, the server checks whether the submitted `resolution_code` and `evidence_ref` match the stored resolution.
   - If they match, the server returns the existing task object with `200 OK` (not `409 Conflict`), acknowledging that the desired state was already achieved.
   - If the task is `COMPLETED` with *conflicting* resolution data, the server returns `409 Conflict` with an explicit error message explaining the discrepancy.
   - No duplicate audit event is written for the repeated idempotent retry.
