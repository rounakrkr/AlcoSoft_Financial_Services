# AUDIT FIX CHANGELOG — AlcoSoft Financial Services
## Production Hardening & Audit Remediation

---

## F002 (v1 — SUPERSEDED)

**Severity:** CRITICAL

**Status:** FIXED ✅ (Revised — prior implementation reclassified as SUPERSEDED)

---

### Prior Implementation — SUPERSEDED (Abort-on-SL-Failure)

**Why it was wrong:**  
The first implementation raised `OrderExecutionError` after SL-M failure, which:
1. Attempted to cancel an already-filled BUY order (exchange rejects cancels on filled orders).
2. Prevented `save_open_position()` from executing — creating an orphan broker position with zero local visibility.
3. Tripped the order circuit breaker on what is a broker SL infrastructure problem, not a trade logic failure.
4. Eliminated all 5 software protection layers (software SL, trailing SL, profit target, EOD squareoff, emergency squareoff) — strictly worse than the original WARNING-only behavior.

---

### Root Cause (Confirmed)

In `_place_buy_order_impl()`, after a BUY order is confirmed filled, SL-M placement failure was treated as non-fatal (WARNING log only) — leading to a locally-tracked live position with `sl_order_id=None` and no path to recover the broker-side SL during the current session. The position was tracked by software SL correctly, but operator visibility was at WARNING severity (easily missed) and there was no automatic retry mechanism.

The real root causes are:
1. **Alert severity was wrong**: WARNING should have been CRITICAL.
2. **No retry mechanism existed**: A transient broker SL failure had no recovery path.
3. **No operator-visible state flag**: The degraded protection state was not queryable.

---

### Revised Fix — Implemented

**Design:** Degraded-but-tracked operation with maximum operator visibility and automatic recovery.

**Principle:** A position saved locally with software SL is better protected than an orphan broker position. Never create orphan positions. Never sacrifice local tracking for the illusion of aborting a filled trade.

---

### Files Modified

- `core/order_executor.py`
- `core/state_manager.py`
- `core/strategy.py`

---

### Functions Modified

| File | Function | Change |
|------|----------|--------|
| `core/order_executor.py` | `_place_buy_order_impl()` | Replaced abort-on-failure with retry + CRITICAL alert + state flag |
| `core/order_executor.py` | `attempt_broker_sl_recovery()` | **NEW** — loop-level SL recovery function |
| `core/state_manager.py` | `update_position_notes()` | **NEW** — update notes field for open position |
| `core/strategy.py` | `_check_all_exits()` | Added `attempt_broker_sl_recovery()` as first call |
| `core/strategy.py` | import block | Added `attempt_broker_sl_recovery` to import |

---

### Exact Changes

#### 1. `_place_buy_order_impl()` — SL-M placement section

**Old behavior (WARNING only):**
```python
if not trade["sl_order_id"]:
    logger.warning("⚠️ Kotak SL-M FAILED ...")
    # position saved silently with sl_order_id=None
```

**New behavior:**
- 3 external retry attempts, 2 seconds apart (each internal call has 2 internal retries = up to 6 total broker API calls)
- On total failure: position saved locally with `notes="SL_BROKER_UNPROTECTED"` flag
- `CRITICAL` log (not WARNING) with full order context
- Immediate Slack CRITICAL alert to operator
- Permanent audit trail entry via `audit_system_error()`
- `OrderExecutionError` is **NOT raised** — circuit breaker is **not tripped** — local position tracking is **preserved**

#### 2. `attempt_broker_sl_recovery()` — NEW function in `order_executor.py`

```
Called by: _check_all_exits() → every 5-second strategy loop iteration
Condition: TRADING_MODE == "LIVE" only
Logic:
  - Iterates all open positions
  - Skips positions with kotak_sl_order_id already set
  - Skips positions WITHOUT "SL_BROKER_UNPROTECTED" in notes
  - For matching positions: calls _send_kotak_sl_order() with active SL level
    (uses max(stop_loss, trailing_sl) to respect TSL ratchet)
  - On success: calls update_sl_order_id() + update_position_notes("") to clear flag
    Sends INFO Slack alert confirming recovery
  - On failure: CRITICAL log only, will retry next iteration
```

#### 3. `update_position_notes()` — NEW function in `state_manager.py`

Simple UPDATE on `trades.notes WHERE symbol=? AND status='OPEN'`. Used by the recovery function to clear the `SL_BROKER_UNPROTECTED` flag after successful broker SL placement.

#### 4. `_check_all_exits()` — strategy.py

```python
def _check_all_exits(live_prices):
    attempt_broker_sl_recovery()  # ← NEW: first action every loop
    check_stop_losses(live_prices)
    update_trailing_stop_losses(live_prices)
    ...
```

---

### Validation Performed

| Test | Result |
|------|--------|
| Syntax check: `order_executor.py`, `state_manager.py`, `strategy.py` | ✅ PASS |
| Import check: `attempt_broker_sl_recovery` from `order_executor` | ✅ PASS |
| Import check: `update_position_notes`, `update_sl_order_id` from `state_manager` | ✅ PASS |
| Import chain: `core.strategy` imports all correctly | ✅ PASS |
| Test A: PAPER mode — recovery is no-op | ✅ PASS |
| Test B: LIVE mode — no positions → silent | ✅ PASS |
| Test C: Position with existing SL order — skipped | ✅ PASS |
| Test D: Position without `SL_BROKER_UNPROTECTED` note — skipped | ✅ PASS |
| Test E: Unprotected position — triggers recovery, broker returns None → CRITICAL log | ✅ PASS |
| Test F: Unprotected position — broker returns order ID → state updated, INFO alert | ✅ PASS |

---

### Remaining Risks

1. **Filled BUY with SL-M failure AND process crash before recovery**: Position exists on broker, software SL cannot fire during downtime. Only Kotak's auto-squareoff (~3:20 PM) provides protection. This is the original unavoidable risk. It is now clearly communicated via CRITICAL alert rather than being silent.
2. **Recovery attempts every 5 seconds may spam broker API** if the SL infrastructure is down for extended periods. Mitigation: `_send_kotak_sl_order` has its own internal 2-retry loop; each call is bounded.
3. **`notes` field is a free-text field** — the `SL_BROKER_UNPROTECTED` flag check uses substring matching. If notes are overwritten by another code path, the flag may be lost. This is acceptable: the position would then not be a candidate for recovery even if it still lacks an SL order. Risk is low given no other code currently writes to notes for open positions.

---

### Rollback Notes

To revert to original WARNING-only behavior (before any F002 changes):
1. Revert `_place_buy_order_impl()` to single-attempt SL placement with WARNING log.
2. Remove `attempt_broker_sl_recovery()` from `order_executor.py`.
3. Remove `update_position_notes()` from `state_manager.py`.
4. Remove `attempt_broker_sl_recovery()` call from `_check_all_exits()` in `strategy.py`.
5. Remove `attempt_broker_sl_recovery` from the import block in `strategy.py`.

---

## F002 (v2 — CURRENT) ✅

**Severity:** CRITICAL
**Status:** FIXED AND VERIFIED

### What Changed vs v1

v1 of the recovery function called `_send_kotak_sl_order()` on every 5-second strategy loop iteration for every unprotected position. Under a 30-minute broker outage with 10 positions this generated **7,200 broker API calls**, introducing three new failure modes: rate limiting, order circuit breaker trips blocking legitimate sells, and CRITICAL log flooding at 480 lines/minute.

### Mitigations Implemented

#### 1. Circuit-Breaker Guard (highest priority)
`attempt_broker_sl_recovery()` checks `get_breaker("order").is_open()` as its first action. If the circuit is open, all recovery is skipped with a single DEBUG log. Recovery never contributes to circuit trips; circuit trips do not worsen the unprotected state.

#### 2. Per-Symbol Exponential Backoff
Module-level `_sl_recovery_state: dict[str, dict]` tracks `last_attempt_ts`, `next_delay_sec`, `attempt_count` per symbol. Initial delay 30s, doubles each failure, caps at 300s. State updated **before** the broker call so exceptions advance the backoff.

Worst-case schedule per symbol (30-min outage): attempts at t=30, 90, 210, 450, 750, 1050, 1350, 1650 seconds.
**8 attempts x 10 positions x 2 internal retries = 160 total broker API calls** vs. 7,200 unbounded. **Reduction: 97.8%.**

On success: symbol removed from dict (implicit reset). On restart: `last_attempt_ts=0` means first recovery fires ~30s after process start (correct).

#### 3. Log-Flood Protection
- Cooldown skips: `DEBUG` (invisible at WARNING+ threshold)
- Circuit open skips: `DEBUG`
- Each actual attempt: `WARNING`
- Attempt #1 failure: `CRITICAL` (once)
- Every 5th failure: `WARNING` summary
- All other failures: `DEBUG`
- Success: `INFO`

Result: during a 30-min, 8-attempt outage: **1 CRITICAL + 1-2 WARNING** per symbol. No flood.

### Files Modified
`core/order_executor.py` only — module-level constants (`_SL_RECOVERY_INITIAL_DELAY_SEC=30`, `_SL_RECOVERY_MAX_DELAY_SEC=300`, `_SL_RECOVERY_BACKOFF_FACTOR=2`, `_SL_RECOVERY_LOG_EVERY_N=5`) and `_sl_recovery_state` dict + revised `attempt_broker_sl_recovery()`.

### Validation — 10/10 PASS

| Test | Result |
|------|--------|
| Syntax: all 3 modified files | PASS |
| TEST 1: PAPER mode no-op | PASS |
| TEST 2: First call fires immediately | PASS |
| TEST 3: Second immediate call blocked by cooldown | PASS |
| TEST 4: Backoff doubles 30s to 60s after failure | PASS |
| TEST 5: Call fires after cooldown elapsed | PASS |
| TEST 6: Backoff caps at 300s | PASS |
| TEST 7: Circuit OPEN prevents ALL broker calls | PASS |
| TEST 8: Success clears state and updates DB | PASS |
| TEST 9: Protected position silently skipped | PASS |
| TEST 10: Load math confirmed: 160 calls vs. 7,200 (97.8% reduction) | PASS |

Test file: `tests/test_sl_recovery_v2.py`

---

## F002 (v3 — FINAL) ✅

**Severity:** CRITICAL
**Status:** FINALIZED

### Problem with v2 State Representation

v2 wrote `trade["notes"] = "SL_BROKER_UNPROTECTED"` at trade entry and parsed `"SL_BROKER_UNPROTECTED" in notes` to determine recovery eligibility. This stored **machine state in a free-text operator field**.

Three structural problems:

1. **Scenario C clobber:** `update_open_position_from_broker()` in reconciliation writes `"notes": "Broker reconciliation repaired..."` for any position with a quantity/entry mismatch. This silently overwrites the flag, making the position invisible to the recovery loop while `kotak_sl_order_id` remains empty. Recovery silently stops.

2. **Future corruption risk:** Any future code that annotates `notes` for any legitimate reason (dashboard annotations, admin tools, further reconciliation passes) becomes an inadvertent recovery disabler. The field has no schema enforcement and no reserved semantics.

3. **Substring fragility:** Truncation, encoding anomalies, or partial writes could silently disable recovery with no observable error.

### Root Cause

Machine state was stored in a field designed for operator commentary. `kotak_sl_order_id` is already the authoritative field for broker SL state — it was not being used as the sole recovery signal.

### Fix

Remove the notes flag entirely. Recovery eligibility is now determined solely by `kotak_sl_order_id` being empty in LIVE mode. This is:
- **Authoritative:** It is exactly the field that becomes non-empty when recovery succeeds
- **Durable:** Persists across restarts in SQLite; not subject to accidental overwrite by reconciliation
- **Immune to Scenario C:** `update_open_position_from_broker()` does not clear `kotak_sl_order_id` in the reconciliation path

### Changes

`core/order_executor.py` only:
1. `_place_buy_order_impl()` — removed `trade["notes"] = "SL_BROKER_UNPROTECTED"` line
2. `attempt_broker_sl_recovery()` — removed `notes` variable and `"SL_BROKER_UNPROTECTED" not in notes` check; guard is now simply `if sl_order_id: continue`
3. `attempt_broker_sl_recovery()` success path — removed `update_position_notes(symbol, "")` call (no flag to clear)
4. Docstring updated to v3 with corrected load numbers

### Validation — 10/10 PASS

| Test | Result |
|------|--------|
| TEST 1: Empty kotak_sl_order_id triggers recovery | PASS |
| TEST 2: Populated kotak_sl_order_id is silently skipped | PASS |
| TEST 3: Scenario C immunity — notes clobber does not disable recovery | PASS |
| TEST 4: Old SL_BROKER_UNPROTECTED note is inert when sl_order_id is set | PASS |
| TEST 5: PAPER mode no-op | PASS |
| TEST 6: Circuit OPEN prevents all recovery | PASS |
| TEST 7: Exponential backoff still operates correctly | PASS |
| TEST 8: Success sets sl_order_id — position silently exits recovery | PASS |
| TEST 9: update_position_notes not called (notes no longer used as state) | PASS |
| TEST 10: Mixed positions — only unprotected ones trigger recovery | PASS |

Test file: `tests/test_sl_recovery_v2.py`

### F002 is now closed.

---

## F005 - Capital API Failure Observability

**Severity:** CRITICAL
**Category:** Capital / P&L Accounting
**Status:** FIXED AND VERIFIED

### Finding

`_get_available_capital()` in LIVE mode: if `client.limits()` returns `None`, the subsequent `.get("Net")` raises `AttributeError`, caught silently by bare `except Exception`, returning `_capital_cache` whose initial value is `0.0`. This propagates through quantity calculation to `qty=0` for all new orders. No alert fires. Two pre-declared module-level globals (`_capital_api_failures`, `_CAPITAL_API_FAILURE_ALERT_THRESHOLD = 3`) were dead code â€” declared but never referenced.

### Fix

Wired up `_capital_api_failures` counter. Added two CRITICAL alert paths:

1. **Zero-cache path:** Any failure when `_capital_cache == 0.0` fires CRITICAL + Slack on first occurrence. Cache has never been populated â€” all buy orders return `qty=0`.
2. **Threshold path:** CRITICAL + Slack when consecutive failures reach `_CAPITAL_API_FAILURE_ALERT_THRESHOLD` (default 3). Cache is warm but stale.

Also: non-dict API responses now raise `ValueError` explicitly; zero/empty field responses raise `ValueError` with field values; periodic CRITICAL every 5th failure past threshold (no flood); INFO log on recovery with failure count; market-closed `stCode==300015` does not increment failure counter.

### Files Modified

`core/order_executor.py` â€” `_get_available_capital()` only.

### Validation â€” 10/10 PASS

| Test | Result |
|------|--------|
| TEST 1: Successful fetch resets counter and updates cache | PASS |
| TEST 2: None response with zero cache â€” CRITICAL immediate | PASS |
| TEST 3: Failure below threshold with warm cache â€” no alert | PASS |
| TEST 4: Threshold crossed â€” CRITICAL alert fired | PASS |
| TEST 5: Exception with zero cache â€” CRITICAL immediate | PASS |
| TEST 6: Cache hit within TTL â€” no broker call | PASS |
| TEST 7: force_refresh=True bypasses TTL | PASS |
| TEST 8: PAPER mode routes to paper capital function | PASS |
| TEST 9: Market-closed stCode â€” no failure count | PASS |
| TEST 10: Recovery resets failure counter | PASS |

Test file: `tests/test_f005_capital_api.py`

---
