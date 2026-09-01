# Data Provenance — Source, Flow, and Boundaries

**Purpose**: Single source of truth for how every financial number in PulseRecover traces back to its origin.

---

## Provenance Model

Every commerce row (payment, order, customer, subscription, merchant) carries:

| Column | Type | Description |
|--------|------|-------------|
| `source_type` | VARCHAR(32) | `razorpay_test` \| `razorpay_live` \| `simulator` |
| `source_system` | VARCHAR(64) | `razorpay` \| `pulserecover-simulator` |
| `external_id` | VARCHAR(64) | Upstream ID (Razorpay `pay_*`/`order_*`/`sub_*` or simulator deterministic ID) |
| `ingested_at` | TIMESTAMPTZ | When row entered PulseRecover DB (immutable after insert) |
| `created_at` | TIMESTAMPTZ | Gateway timestamp (simulated window for simulator) |

**Derived tables** (incidents, diagnoses, recovery_actions, audit_logs, etc.) carry:
| Column | Type | Description |
|--------|------|-------------|
| `environment` | VARCHAR(16) | `real_test` \| `research` (explicit, indexed) |

---

## Environment Mapping

```
source_type           → environment
─────────────────────────────────────
razorpay_test         → real_test
razorpay_live         → real_test
simulator             → research
```

Enforced by `source_types_for_environment()` in `app/models/base.py:62`.

---

## Real Data Sources (real_test)

### 1. Razorpay Test Mode API (Sync)
- **Trigger**: `POST /api/v1/merchant/sync` (manual or scheduled)
- **Flow**: Razorpay API → `RazorpayReadClient` → `normalize_*()` → upsert on `(source_type, external_id)`
- **Provenance**: `source_type=razorpay_test`, `source_system=razorpay`, `external_id=pay_*/order_*/sub_*`
- **Idempotency**: Upsert key prevents duplicates; re-sync updates in place
- **Quarantine**: Invalid entities skipped, recorded in `sync_runs.entity_counts.errors`
- **Audit**: `sync_runs` row per pass + `audit_logs` for connection state changes

### 2. Razorpay Webhooks (Real-time)
- **Endpoint**: `POST /webhooks/razorpay`
- **Auth**: HMAC-SHA256 verification (fail-closed)
- **Dedupe**: `X-Razorpay-Event-Id` → `WebhookEvent.gateway_event_id` UNIQUE
- **Flow**: Verify → Persist raw → Dispatch handlers → Update payment state → Audit
- **Provenance**: `WebhookEvent.source=razorpay`, `signature_valid=true`
- **Handlers**: `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, `subscription.charged`, `subscription.charge_failed`
- **Out-of-order safe**: Payment state machine handles late/duplicate events

### 3. Recovery Actions (Real Execution)
- **Trigger**: Policy decision → `RecoveryExecutor` → `RazorpayGateway`
- **Actions**: `CREATE_PAYMENT_LINK`, `CREATE_ORDER`, `CREATE_SUBSCRIPTION`, `RETRY_PAYMENT`
- **Provenance**: `RecoveryAction.gateway_request_id` = our idempotency key = Razorpay `reference_id`/`receipt`
- **Verification**: Webhook or polling → `RECOVERED` status + `verified_at`
- **Audit**: Every transition logged to `audit_logs` with actor, request_id, external IDs

---

## Synthetic Data Sources (research)

### 1. Simulator Engine
- **Trigger**: `scripts/seed.py` or `POST /api/v1/demo/scenario/{name}`
- **Flow**: `SimulatorConfig` → `run_simulation()` → generates merchants, customers, orders, payments, events
- **Provenance**: `source_type=simulator`, `source_system=pulserecover-simulator`
- **Determinism**: Same `seed` + `config` → identical dataset (config_hash = run_id)
- **Idempotency**: `SimulatorRun` row with deterministic `run_id` — re-seed is no-op
- **Ground Truth**: `SimulatorGroundTruth` table links injected incidents to detection results

### 2. Simulator Webhooks
- **Trigger**: Scenario runner or manual
- **Flow**: `SimulatedPaymentGateway` → generates webhook payloads → `POST /webhooks/razorpay` (source=simulator)
- **Provenance**: `WebhookEvent.source=simulator`
- **Isolation**: Only affects `research` environment derived tables

### 3. Evaluation Runs
- **Trigger**: `POST /api/v1/evaluation/runs` (Research Lab UI)
- **Flow**: Synthetic dataset → Detector → Diagnosis → Recovery → Compare vs Ground Truth
- **Storage**: `evaluation_runs`, `experiments`, `model_predictions`
- **Persistence**: **Never deleted** by demo reset (scientific record)

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           PULSERECOVER DATA FLOW                              │
└─────────────────────────────────────────────────────────────────────────────┘

REAL MERCHANT MODE (real_test)                          RESEARCH LAB (research)
─────────────────────────────────                       ────────────────────────
                                                                                
┌──────────────┐                                        ┌──────────────┐       
│ Razorpay     │                                        │ Simulator    │       
│ Test Mode    │                                        │ Config       │       
│ API          │                                        │ (seed, days, │       
│              │                                        │  incidents)  │       
└──────┬───────┘                                        └──────┬───────┘       
       │                                                     │               
       ▼                                                     ▼               
┌──────────────────────┐                         ┌──────────────────────┐   
│ RazorpayReadClient   │                         │ Simulator Engine     │   
│ (GET orders/payments │                         │ (generates commerce  │   
│  subscriptions)      │                         │  + payment_events)   │   
└──────┬───────────────┘                         └──────────┬───────────┘   
       │                                                    │              
       ▼                                                    ▼              
┌──────────────────────┐                         ┌──────────────────────┐   
│ normalize_*()        │                         │ SimResult            │   
│ validate + transform │                         │ (merchants, orders,  │   
└──────┬───────────────┘                         │  payments, events)   │   
       │                                         └──────────┬───────────┘   
       ▼                                                    │              
┌──────────────────────┐                                    │              
│ UPSERT on            │                                    ▼              
│ (source_type,        │                         ┌──────────────────────┐   
│  external_id)        │                         │ SimulatorGroundTruth │   
│                      │                         │ (injected incidents) │   
│ source_type=         │                         └──────────┬───────────┘   
│ razorpay_test        │                                    │              
│ source_system=razorpay                                    ▼              
└──────┬───────────────┘                         ┌──────────────────────┐   
       │                                         │ Anchored Detection   │   
       ▼                                         │ Pass (research env)  │   
┌──────────────────────┐                         └──────────┬───────────┘   
│ Database             │                                    │              
│ (payments, orders,   │                                    ▼              
│  customers, ...)     │                         ┌──────────────────────┐   
│                      │                         │ Incidents (research) │   
└──────┬───────────────┘                         │ Diagnoses (research) │   
       │                                         │ Recovery (research)  │   
       ▼                                         └──────────────────────┘   
┌──────────────────────┐                                         
│ Webhook Intake       │                                         
│ (HMAC verify,        │                                         
│  dedupe, dispatch)   │                                         
│                      │                                         
│ WebhookEvent.source= │                                         
│ razorpay             │                                         
└──────┬───────────────┘                                         
       │                                                         
       ▼                                                         
┌──────────────────────┐                                         
│ Payment State        │                                         
│ Machine              │                                         
│ (updates payment     │                                         
│  status, creates      │                                         
│  recovery opps)      │                                         
└──────┬───────────────┘                                         
       │                                                         
       ▼                                                         
┌──────────────────────┐                                         
│ Recovery Executor    │                                         
│ (RazorpayGateway)    │                                         
│                      │                                         
│ Creates Payment Link │                                         
│ gateway_request_id   │                                         
│ = reference_id       │                                         
└──────┬───────────────┘                                         
       │                                                         
       ▼                                                         
┌──────────────────────┐                                         
│ Verification         │                                         
│ (webhook/poll)       │                                         
│ → RECOVERED          │                                         
└──────┬───────────────┘                                         
       │                                                         
       ▼                                                         
┌──────────────────────┐                                         
│ Audit Trail          │                                         
│ (every transition)   │                                         
└──────────────────────┘                                         

SHARED INTELLIGENCE LAYER (environment-agnostic)
──────────────────────────────────────────────
Detection Engine → Diagnosis Service → Policy Engine → Recovery Builder
     │                │                    │                │
     ▼                ▼                    ▼                ▼
Scopes to          Queries real          Evaluates       Builds
environment        merchant data         policy on       strategies
(via source_type)  only                 real data       for real env
```

---

## Provenance in UI

### Dashboard (Command Center)
- **ProvenanceChip**: Shows "Razorpay Test Mode" + record count + time window
- **MetricStrip**: Every number sourced from `DashboardSummary` (environment-scoped query)
- **Empty State**: "Connect Razorpay Test Mode to begin" — no fake data

### Payments Page
- **Source Badge**: Each payment shows `razorpay test` / `razorpay live` / `research`
- **Filter**: Environment switcher scopes entire view

### Incidents
- **Incident Card**: Shows `environment` badge (Real Test / Research)
- **Diagnosis**: `AgentReport` cites specific payment/order IDs as evidence
- **Recovery**: `RecoveryOpportunity` scoped to incident's environment

### Recovery
- **Pipeline**: Shows `environment` badge per opportunity/action
- **Action Detail**: `gateway_request_id` links to Razorpay entity
- **Verification**: Webhook `event_id` + `verified_at` timestamp

### Audit Trail
- **Filter**: Environment tabs (Real Test / Research)
- **Row Badge**: `ENVIRONMENT_BADGE_LABEL` (Real Test / Research)
- **Source Chip**: `sourceTypeLabel()` on commerce entities
- **Demo Reset**: Only appears in Research tab (action=`demo.reset`, environment=`research`)

### Settings
- **Connection Status**: Live probe result (configured/connected/environment)
- **Key ID**: Masked (`rzp_test_••••ab12`)
- **Sync History**: `sync_runs` with per-entity counts + quarantine errors

---

## Provenance Guarantees

| Guarantee | Enforcement |
|-----------|-------------|
| No simulator data in real_test queries | `WHERE source_type IN ('razorpay_test','razorpay_live')` |
| No real data in research queries | `WHERE source_type = 'simulator'` |
| Derived tables scoped | `WHERE environment = 'real_test' OR 'research'` |
| Webhooks tagged | `source` column = `razorpay` \| `simulator` |
| Recovery actions scoped | `RecoveryAction.environment` column |
| Audit trail separated | `AuditLog.environment` column |
| Audit trail tamper-evident | sha256 chain (`previous_hash`/`entry_hash`, stamped at flush) + `GET /api/v1/audit/verify` |
| Demo reset never touches real_test | `WHERE source_type='simulator'` + `environment='research'` |
| Evaluation runs preserved | `_KEPT_TABLES` excludes them from reset |

---

## Provenance for Every Financial Number

| Metric | Source | Provenance Exposed |
|--------|--------|-------------------|
| Revenue processed | `Payment.amount_paise` (success) | `source_type`, `ingested_at`, `external_id` |
| Success rate | `Payment.status` (captured/failed) | Data-anchored window, baseline window |
| Revenue at risk | `RevenueService.revenue_at_risk()` per incident | `Incident.revenue_at_risk_paise` + audit log on refresh |
| Recovered revenue | `RecoveryAction.amount_paise` (status=RECOVERED) | `verified_at`, `gateway_request_id`, webhook `event_id` |
| Lost revenue | Terminal incidents: `observed_loss - actual_recovered` | `Incident` + `RecoveryAction` linkage |
| Recovery rate | `recovered / (recovered + lost + at_risk)` | All components traced above |

---

## Database Schema References

### Commerce Tables (no environment column)
- `merchants` — `source_type`, `source_system`, `external_id`, `ingested_at`
- `customers` — `source_type`, `source_system`, `external_id`, `ingested_at`, `merchant_id`
- `orders` — `source_type`, `source_system`, `external_id`, `ingested_at`, `merchant_id`, `amount_paise`, `currency`, `status`, `created_at`
- `payments` — `source_type`, `source_system`, `external_id`, `ingested_at`, `merchant_id`, `order_id`, `amount_paise`, `currency`, `status`, `method`, `captured`, `created_at`
- `subscriptions` — `source_type`, `source_system`, `external_id`, `ingested_at`, `merchant_id`, `plan_id`, `customer_id`, `status`, `created_at`
- `payment_events` — `source_type`, `source_system`, `external_id`, `ingested_at`, `payment_id`, `event_type`, `status`, `amount_paise`, `created_at`

### Derived Tables (explicit environment column)
- `incidents` — `environment`, `metric`, `severity`, `status`, `detected_at`, `revenue_at_risk_paise`
- `incident_evidence` — `environment`, `incident_id`, `evidence_type`, `payload`
- `diagnoses` — `environment`, `incident_id`, `cause`, `confidence`, `reasoning`
- `recovery_opportunities` — `environment`, `incident_id`, `strategy_type`, `status`
- `recovery_strategies` — scoped via `opportunity_id` → `environment`
- `recovery_actions` — `environment`, `gateway_request_id`, `action_type`, `status`, `amount_paise`, `verified_at`
- `policy_decisions` — scoped via `action_id` → `environment`
- `agent_reports` — `environment`, `incident_id`, `diagnosis_id`, `opportunity_id`, `reasoning`
- `audit_logs` — `environment`, `actor`, `action`, `entity_type`, `entity_id`, `details`, `previous_hash`/`entry_hash` (sha256 hash chain — tamper-evidence, see below)
- `webhook_events` — `source` (`razorpay`/`simulator`), `gateway_event_id`, `event_type`, `processed`

### Simulator Tables (research only)
- `simulator_runs` — `run_id` (deterministic), `config_hash`, `scenario`, `status`, `stats`
- `simulator_ground_truth` — `simulator_run_id`, `entity_type`, `truth` (JSON)

### Sync & Connection
- `sync_runs` — `status`, `actor`, `entity_counts` (JSON), `error`
- `connection_state` — singleton: `sync_enabled`, `last_sync_at`, `last_webhook_at`, `last_sync_status`

---

## Verification Queries

### Verify No Leakage (Real Test)
```sql
-- Should return 0
SELECT COUNT(*) FROM payments WHERE source_type = 'simulator';
SELECT COUNT(*) FROM incidents WHERE environment = 'research';
SELECT COUNT(*) FROM recovery_actions WHERE environment = 'research';
```

### Verify No Leakage (Research)
```sql
-- Should return 0
SELECT COUNT(*) FROM payments WHERE source_type IN ('razorpay_test', 'razorpay_live');
SELECT COUNT(*) FROM incidents WHERE environment = 'real_test';
SELECT COUNT(*) FROM recovery_actions WHERE environment = 'real_test';
```

### Provenance Audit
```sql
-- Every payment traced to source
SELECT source_type, source_system, external_id, ingested_at, created_at
FROM payments
ORDER BY ingested_at DESC;

-- Every recovery action traced to gateway
SELECT environment, gateway_request_id, action_type, status, amount_paise, verified_at
FROM recovery_actions
ORDER BY created_at DESC;
```

### Verify Audit Chain Integrity
```
GET /api/v1/audit/verify
-- {"valid": true, "checked": N, "chained": M, "legacy": K, "first_bad_id": null}
```
The audit trail is hash-chained at insert time (`app.models.system` flush
hook): each row's `previous_hash` is the previous row's `entry_hash` (sha256
over id, ts, actor, action, entity, details, previous_hash). The verifier
replays the whole table — deliberately NOT environment-scoped, since the
chain spans both environments in insertion order — recomputing digests and
localizing the first tampered/gapped row. Pre-chain rows (NULL hashes) pass
as legacy-valid. Tamper-evidence, not tamper-proof: limits and the
single-node assumption are documented in `docs/security-testing.md`.

---

*Generated: 2026-08-30*
*Matches implementation as of PulseRecover v0.1.0*