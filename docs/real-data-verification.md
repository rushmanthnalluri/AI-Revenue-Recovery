# Real Data Verification — Reviewer Test Evidence

**Purpose**: Document the exact steps and expected outcomes for a reviewer to verify the real Razorpay Test Mode flow end-to-end.

**Status**: Template — fill in during manual verification with real Razorpay test credentials.

---

## Prerequisites

- [ ] Fresh database (`pulserecover.db` deleted or new path)
- [ ] Valid Razorpay Test Mode API keys (`rzp_test_...`)
- [ ] Razorpay Test Mode webhook secret configured in Dashboard
- [ ] Backend `.env` configured with:
  ```bash
  RAZORPAY_KEY_ID=rzp_test_...
  RAZORPAY_KEY_SECRET=...
  RAZORPAY_WEBHOOK_SECRET=...
  RAZORPAY_BASE_URL=https://api.razorpay.com/v1
  SIMULATION_MODE=false
  ```
- [ ] Frontend running on `http://localhost:3000`
- [ ] Backend running on `http://localhost:8000`

---

## Test Sequence

### Step 1: Fresh Start — Empty Database

**Action**: Start backend with fresh DB (no migrations run yet, or `alembic upgrade head` on empty DB)

**Expected**:
- [ ] Backend starts without errors
- [ ] Tables created via `Base.metadata.create_all()` (or Alembic)
- [ ] No `simulator_runs` rows
- [ ] No `payments`/`orders`/`customers` rows
- [ ] `connection_state` singleton created (sync_enabled=false by default)

**Evidence**:
```bash
# Check tables
sqlite3 pulserecover.db ".tables"

# Verify empty commerce
sqlite3 pulserecover.db "SELECT COUNT(*) FROM payments;"
sqlite3 pulserecover.db "SELECT COUNT(*) FROM orders;"
sqlite3 pulserecover.db "SELECT COUNT(*) FROM simulator_runs;"
```

---

### Step 2: Open Application — Default Real Merchant Mode

**Action**: Open `http://localhost:3000` in browser

**Expected**:
- [ ] Sidebar shows "Real Merchant" selected (amber accent)
- [ ] Topbar shows "Razorpay Test Mode · Not connected" (amber badge)
- [ ] Command Center shows empty state: "Connect Razorpay Test Mode to begin"
- [ ] No payment data, no incidents, no recovery pipeline
- [ ] Navigation: Console (Command Center, Payments, Incidents, Recovery, Audit) + Workspace (Research Lab, Settings)

**Evidence**: Screenshot of Command Center empty state

---

### Step 3: Configure Connection — Settings Page

**Action**: Navigate to Settings (`/settings`)

**Expected**:
- [ ] "Razorpay connection" section shows:
  - Environment: "Razorpay Test Mode"
  - Connection: "Not connected" (red dot)
  - Key ID: masked (e.g., `rzp_test_••••ab12`)
  - Key secret: `••••••••` (never revealed)
  - Webhook: "Configured" (if `RAZORPAY_WEBHOOK_SECRET` set) or "Not configured"
  - Auto sync: "Disabled"
  - Last sync: "never"
  - Last webhook: "none received"
- [ ] "Sync now" button disabled (not connected)
- [ ] "Test webhook" button available
- [ ] "Connect" button available (since `sync_enabled=false`)

**Evidence**: Screenshot of Settings page

---

### Step 4: Enable Sync (Connect)

**Action**: Click "Connect" button in Settings

**Expected**:
- [ ] POST `/api/v1/merchant/sync/enable` succeeds
- [ ] `sync_enabled` flips to `true`
- [ ] Audit log entry: `action=merchant.sync_enable`
- [ ] UI updates: "Auto sync: Enabled", "Disconnect" button shown

**Evidence**: Network tab showing 200 response + UI update

---

### Step 5: Initial Sync

**Action**: Click "Sync now" button in Settings

**Expected**:
- [ ] POST `/api/v1/merchant/sync` executes
- [ ] Sync runs: orders → payments → payment_links → subscriptions
- [ ] Response includes `entity_counts` with created/updated counts
- [ ] `sync_runs` row created with `status=completed`
- [ ] `connection_state.last_sync_at` updated
- [ ] `connection_state.last_sync_status = "completed"`

**Evidence**: 
```bash
# Check sync run
sqlite3 pulserecover.db "SELECT * FROM sync_runs ORDER BY created_at DESC LIMIT 1;"

# Check ingested entities
sqlite3 pulserecover.db "SELECT source_type, COUNT(*) FROM payments GROUP BY source_type;"
sqlite3 pulserecover.db "SELECT source_type, COUNT(*) FROM orders GROUP BY source_type;"
sqlite3 pulserecover.db "SELECT source_type, COUNT(*) FROM customers GROUP BY source_type;"
```

---

### Step 6: Verify Dashboard Updates

**Action**: Navigate to Command Center (`/`)

**Expected**:
- [ ] Topbar shows "Razorpay Test Mode · Connected" (green badge)
- [ ] Command Center shows real metrics:
  - Revenue processed (₹ from real payments)
  - Payment success rate (from real data)
  - Observed payment count
  - Baseline success rate (if 24h+ data)
- [ ] No "Simulation" or "Demo" labels anywhere
- [ ] Provenance chip: "Razorpay Test Mode · N records · 1h window"

**Evidence**: Screenshot of populated Command Center

---

### Step 7: Create Real Test Payment

**Action**: In Razorpay Dashboard (Test Mode):
1. Create a Payment Link or Order
2. Complete test payment (use test card: `4111 1111 1111 1111`, any future expiry, any CVV)

**Expected**:
- [ ] Payment appears in Razorpay Dashboard (Test Mode)
- [ ] Webhook received by PulseRecover (`POST /webhooks/razorpay`)
- [ ] Webhook signature verified (check logs)
- [ ] Payment status updated in PulseRecover DB
- [ ] `WebhookEvent` row created with `source=razorpay`, `signature_valid=true`

**Evidence**:
```bash
# Check webhook event
sqlite3 pulserecover.db "SELECT * FROM webhook_events WHERE source='razorpay' ORDER BY received_at DESC LIMIT 5;"

# Check payment updated
sqlite3 pulserecover.db "SELECT external_id, status, amount_paise, ingested_at FROM payments WHERE source_type='razorpay_test' ORDER BY ingested_at DESC LIMIT 5;"
```

---

### Step 8: Verify Dashboard Reflects Webhook

**Action**: Refresh Command Center (or wait 15s poll)

**Expected**:
- [ ] Payment count increases
- [ ] Revenue processed updates
- [ ] Success rate recalculates
- [ ] No manual sync required (webhook is real-time)

**Evidence**: Screenshot before/after webhook

---

### Step 9: Trigger Detection (if applicable)

**Action**: If payment failure occurs (or wait for natural degradation):
- Or manually trigger: `POST /api/v1/detection/run` with appropriate window

**Expected**:
- [ ] Detection runs on `real_test` environment data
- [ ] If degradation detected: Incident created with `environment=real_test`
- [ ] Incident appears in Command Center "Recent degradation"
- [ ] Incident detail shows real payment IDs as evidence
- [ ] AI investigation cites real payment/order IDs

**Evidence**: 
```bash
# Check incidents
sqlite3 pulserecover.db "SELECT id, title, environment, metric, severity, detected_at FROM incidents WHERE environment='real_test';"

# Check agent report
sqlite3 pulserecover.db "SELECT * FROM agent_reports WHERE environment='real_test' ORDER BY created_at DESC LIMIT 3;"
```

---

### Step 10: Recovery Action (Payment Link)

**Action**: If incident creates recovery opportunity:
1. Open Incident → "Investigate" → Review AI diagnosis
2. Open Recovery → Approve recommended action (CREATE_PAYMENT_LINK)
3. Execute action

**Expected**:
- [ ] Policy decision recorded: `action=policy.decide`, `decision=approve`
- [ ] `RecoveryAction` created: `action_type=CREATE_PAYMENT_LINK`, `environment=real_test`
- [ ] `RazorpayGateway.create_payment_link()` called with `reference_id=gateway_request_id`
- [ ] Payment Link created in Razorpay Test Mode (verify in Dashboard)
- [ ] `RecoveryAction.gateway_request_id` = Razorpay `reference_id`
- [ ] Action status: `EXECUTING` → `VERIFYING` → `RECOVERED` (on webhook)

**Evidence**:
```bash
# Check recovery action
sqlite3 pulserecover.db "SELECT * FROM recovery_actions WHERE environment='real_test' ORDER BY created_at DESC LIMIT 3;"

# Check audit trail
sqlite3 pulserecover.db "SELECT * FROM audit_logs WHERE environment='real_test' AND action LIKE 'recovery.%' ORDER BY created_at DESC LIMIT 10;"
```

---

### Step 11: Verify Recovery Completion

**Action**: Customer pays via Payment Link (Test Mode)

**Expected**:
- [ ] `payment.captured` webhook received
- [ ] Payment linked to Payment Link via `reference_id`
- [ ] `RecoveryAction` status → `RECOVERED`
- [ ] `verified_at` timestamp set
- [ ] `recovered_revenue_paise` increases on dashboard
- [ ] Audit: `action=recovery.verified`, `details={webhook_event_id, amount_paise}`

**Evidence**: Dashboard shows recovered revenue + audit trail

---

### Step 12: Inspect Full Audit Trail

**Action**: Navigate to Audit Trail (`/audit`)

**Expected**:
- [ ] Tabs: "Real Test" (selected) / "Research"
- [ ] Real Test tab shows: sync, webhook, detection, policy, recovery, verification events
- [ ] Each row: timestamp, actor, action, entity, environment badge "Real Test"
- [ ] **No** `demo.reset` entries in Real Test tab
- [ ] Research tab shows only simulator-seeded events (if any)

**Evidence**: Screenshot of Audit Trail with both tabs

---

### Step 13: Switch to Research Lab

**Action**: Click "Research Lab" in sidebar environment switcher

**Expected**:
- [ ] Topbar strip: "Synthetic research — simulator data, not merchant activity"
- [ ] Sidebar "Research Lab" selected (slate accent)
- [ ] Page shows "Research Lab" header with simulator disclosure banner
- [ ] Tabs: "Scenarios" / "Evaluation"
- [ ] Scenarios: "standard", "storm", "upi_outage_demo", "payday_wave_demo", "quiet"
- [ ] No real merchant data visible

**Evidence**: Screenshot of Research Lab

---

### Step 14: Run Research Scenario

**Action**: Click "Run" on "standard" scenario

**Expected**:
- [ ] POST `/api/v1/demo/scenario/standard` executes
- [ ] Simulator seeds ~65k events over 30 days with 6 incidents
- [ ] Anchored detection pass runs (research environment)
- [ ] Incidents created with `environment=research`
- [ ] Dashboard (if switched back to Real Merchant) **unchanged**
- [ ] Research Lab shows new incidents in Evaluation tab

**Evidence**:
```bash
# Verify isolation
sqlite3 pulserecover.db "SELECT environment, COUNT(*) FROM incidents GROUP BY environment;"
sqlite3 pulserecover.db "SELECT source_type, COUNT(*) FROM payments GROUP BY source_type;"
```

---

### Step 15: Research Reset

**Action**: Click "Reset research data" → Confirm

**Expected**:
- [ ] POST `/api/v1/demo/reset` executes
- [ ] All simulator commerce + research derived rows deleted
- [ ] `evaluation_runs`, `experiments`, `model_predictions` **preserved**
- [ ] Audit: `action=demo.reset`, `environment=research`
- [ ] Real merchant data **untouched**

**Evidence**:
```bash
# Verify real data intact
sqlite3 pulserecover.db "SELECT COUNT(*) FROM payments WHERE source_type='razorpay_test';"
sqlite3 pulserecover.db "SELECT COUNT(*) FROM incidents WHERE environment='real_test';"

# Verify research cleared
sqlite3 pulserecover.db "SELECT COUNT(*) FROM payments WHERE source_type='simulator';"
sqlite3 pulserecover.db "SELECT COUNT(*) FROM incidents WHERE environment='research';"
```

---

## Known Test Mode Limitations (Document During Test)

| Limitation | Observed Impact | Workaround |
|------------|-----------------|------------|
| Order fetch limited to 180 days | Sync window capped | `MAX_WINDOW_DAYS=180` |
| Payment Links no pagination | Must fetch by reference_id | Only our links fetched |
| Subscription no idempotency | Never retry create | Log warning, ledger guards |
| Webhook delivery unordered | Handlers must be OOO-safe | State machine handles |
| Rate limits on large sync | 429 on list endpoints | Backoff + paginate |

---

## Acceptance Checklist

| Criterion | Verified |
|-----------|----------|
| Fresh DB starts empty | [ ] |
| Default environment = Real Merchant | [ ] |
| No fake data on empty state | [ ] |
| Settings shows real connection state | [ ] |
| Sync ingests real Razorpay data | [ ] |
| Provenance on every record | [ ] |
| Webhook real-time updates dashboard | [ ] |
| Detection runs on real data | [ ] |
| AI cites real payment IDs | [ ] |
| Recovery uses real Payment Link API | [ ] |
| Verification via webhook | [ ] |
| Audit trail complete + separated | [ ] |
| Research Lab isolated | [ ] |
| Research scenario runs | [ ] |
| Research reset preserves real data | [ ] |
| No simulator leakage to real_test | [ ] |
| No real leakage to research | [ ] |

---

## Evidence Package

Attach during verification:
- [ ] Screenshots: Empty state, Settings connected, Populated dashboard, Incident detail, Recovery action, Audit trail (both tabs), Research Lab
- [ ] Database query outputs for each verification step
- [ ] Backend logs showing webhook receipt + signature verification
- [ ] Network tab captures for sync, webhook, recovery execution
- [ ] Razorpay Dashboard screenshots showing test payment + payment link

---

*Template generated: 2026-08-30*
*To be filled during manual verification with real Razorpay Test Mode credentials*