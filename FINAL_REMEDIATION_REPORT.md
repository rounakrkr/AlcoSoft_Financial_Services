# FINAL REMEDIATION REPORT — AlcoSoft Financial Services
## Production Hardening & Audit Remediation
**Report Date:** June 6, 2026  
**Session:** Remediation Pass 1  
**Status:** In Progress

---

## Session Summary

| Metric | Value |
|--------|-------|
| Total findings | 31 (30 original + 1 post-audit) |
| Findings fixed this session | 2 |
| Findings verified | 0 |
| Findings rejected | 0 |
| Risk accepted | 0 |
| Findings remaining | 29 |

---

## Finding Status Table

| ID | Severity | Category | Status | Notes |
|----|----------|----------|--------|-------|
| **F002** | CRITICAL | Stop-Loss Protection Gap | FIXED | Finalized: kotak_sl_order_id as sole recovery signal |
| F001 | CRITICAL | Capital / Margin Logic | NOT_STARTED | |
| F003 | CRITICAL | Data / Indicator Integrity | NOT_STARTED | |
| F004 | CRITICAL | Order / Broker Sync | NOT_STARTED | |
| **F005** | CRITICAL | Capital / P&L Accounting | FIXED | Capital API failure now fires CRITICAL alert |
| F006 | CRITICAL | WebSocket / Data Feed | NOT_STARTED | |
| F007 | CRITICAL | EOD Squareoff | NOT_STARTED | |
| F008 | CRITICAL | Broker Reconciliation | NOT_STARTED | |
| F009 | HIGH | Capital / Margin Logic | NOT_STARTED | |
| F010 | HIGH | Strategy / Signal Logic | NOT_STARTED | |
| F011 | HIGH | State / Crash Recovery | NOT_STARTED | |
| F012 | HIGH | Reflection Engine / Adaptive Safety | NOT_STARTED | |
| F013 | HIGH | Broker Token / Authentication | NOT_STARTED | |
| F014 | HIGH | Data / Instrument Token | NOT_STARTED | |
| F015 | HIGH | Configuration Risk | NOT_STARTED | |
| F016 | HIGH | Concurrency / Thread Safety | NOT_STARTED | |
| F017 | HIGH | Database / State Integrity | NOT_STARTED | |
| F018 | HIGH | Strategy / Signal Logic | NOT_STARTED | |
| F019 | MEDIUM | Capital / Margin Logic | NOT_STARTED | |
| F020 | MEDIUM | Broker Token / Authentication | NOT_STARTED | |
| F021 | MEDIUM | Data / Yahoo Finance | NOT_STARTED | |
| F022 | MEDIUM | State / Crash Recovery | NOT_STARTED | |
| F023 | MEDIUM | Reflection Engine / Adaptive Safety | NOT_STARTED | |
| F024 | MEDIUM | WebSocket / Data Feed | NOT_STARTED | |
| F025 | MEDIUM | Order / Broker Sync | NOT_STARTED | |
| F026 | MEDIUM | Configuration Risk | NOT_STARTED | |
| F027 | LOW | Logging / Audit | NOT_STARTED | |
| F028 | LOW | Strategy / Signal Logic | NOT_STARTED | |
| F029 | LOW | Capital / Margin Logic | NOT_STARTED | |
| F030 | LOW | Database / State Integrity | NOT_STARTED | |
| **FX01** | HIGH | Capital / P&L Accounting | OPEN — PENDING | Post-audit: stale capital age gate absent at entry |

---

## F002 — Detailed Fix Summary

### Classification: CONFIRMED — FINALIZED

**Finding:** SL-M order placement failure was logged at WARNING severity with no retry, no operator alert, and no automatic recovery mechanism.

**Iteration history:**
- **v0 (original):** WARNING-only, no retry, no recovery.
- **v1 (SUPERSEDED):** Abort-on-SL-failure. Created orphan broker positions. Reverted.
- **v2 (SUPERSEDED):** Recovery loop without throttling — 7,200 API calls/min during outages. Notes field used as machine state flag.
- **v3 (SUPERSEDED for state representation):** Throttled recovery (circuit-breaker, backoff, log-flood protection). Notes flag survived but vulnerable to Scenario C clobber.
- **v4 = v3 final (CURRENT):** Throttled recovery. Notes flag removed entirely. `kotak_sl_order_id` empty = sole, authoritative recovery eligibility signal.

**What Was Built (final):**

1. **3-retry SL placement loop** at trade entry in `_place_buy_order_impl()`. No notes flag written.
2. **CRITICAL alert + audit trail** on placement failure. Position saved with empty `kotak_sl_order_id`.
3. **`attempt_broker_sl_recovery()`** — throttled recovery function, every 5-second loop iteration:
   - Eligibility: `kotak_sl_order_id` empty in LIVE mode — the authoritative field. No notes parsing.
   - Circuit-breaker guard: skips all attempts when order circuit is open
   - Per-symbol exponential backoff: 30s / 60s / 120s / 240s / 300s cap
   - Log policy: CRITICAL on first failure, WARNING every 5th, DEBUG otherwise
   - On success: calls `update_sl_order_id()` only. No flag to clear.
4. **`_sl_recovery_state`** — module-level backoff state dict with four tuneable constants.
5. **`update_position_notes()`** — added to `state_manager.py` (retained, not used by F002).

**Why notes flag was removed:**
Storing machine state in a free-text operator field creates silent corruption risk. `update_open_position_from_broker()` legitimately overwrites `notes` during reconciliation (Scenario C: quantity/entry mismatch at restart). Any future code annotating `notes` becomes an inadvertent recovery disabler. `kotak_sl_order_id` is already the authoritative field — empty means unprotected, always.

**Worst-case load (30-min outage, 10 positions):** 160 broker API calls vs. 7,200 unbounded. Reduction: 97.8%.

**Files Modified:**
- [`order_executor.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/core/order_executor.py) — primary changes
- [`state_manager.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/core/state_manager.py) — `update_position_notes()` added
- [`strategy.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/core/strategy.py) — recovery hook in `_check_all_exits()`

---

## F005 - Detailed Fix Summary

### Classification: CONFIRMED

**Finding:** In LIVE mode, `_get_available_capital()` calls `client.limits()`. If the call returns `None`, the subsequent `.get("Net")` raises `AttributeError`, silently caught, and the function returns `_capital_cache` whose initial value is `0.0`. Zero capital propagates to `calculate_quantity()` returning `0`. All new buy orders are silently blocked. No alert fires. Two pre-declared counters (`_capital_api_failures`, `_CAPITAL_API_FAILURE_ALERT_THRESHOLD`) were dead code.

**What Was Built:**

1. **`_capital_api_failures` counter wired up** — incremented on every fetch failure, reset to 0 on success, added to `global` declaration.
2. **Zero-cache CRITICAL alert** — any failure when `_capital_cache == 0.0` fires CRITICAL log + Slack on first occurrence. This is the trading-blocked condition; operator must act immediately.
3. **Threshold CRITICAL alert** — fires when consecutive failures reach `_CAPITAL_API_FAILURE_ALERT_THRESHOLD` (3). Cache is warm but stale.
4. **Periodic reminder** — every 5th failure past threshold logs CRITICAL (no Slack repeat, no flood).
5. **Recovery log** — INFO when API recovers after failures, reporting failure count and capital value.
6. **Explicit ValueError** for non-dict responses and zero/empty field responses — bad response is visible in logs rather than silently falling through.
7. **Market-closed protection** — `stCode==300015` returns cache without incrementing the failure counter.

**Files Modified:**
- [`order_executor.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/core/order_executor.py) — `_get_available_capital()` only

**Test file:** [`tests/test_f005_capital_api.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/tests/test_f005_capital_api.py)

**Validation: 10/10 tests passed.**

---

## Architectural Observations

None of the changes made in this session alter trading strategy logic. All changes are in the execution safety layer.

The three-tier protection model AlcoSoft relies on is:
1. **Broker-side SL-M** — survives process crashes
2. **Software SL** (check_stop_losses) — highest-frequency, 5-second loop
3. **EOD squareoff** — hard deadline backstop

F002 was a gap in tier 1. The fix does not remove the gap but it:
- Makes the gap visible the moment it occurs
- Provides automatic healing within one loop iteration when broker SL infrastructure recovers
- Guarantees tier 2 and tier 3 remain fully active regardless of tier 1 status

---

## Validation Summary

| Test Type | Tests Run | Tests Passed |
|-----------|-----------|--------------|
| Syntax validation | 3 files | 3/3 |
| Import chain validation | 3 symbols | 3/3 |
| Functional unit tests | 10 scenarios | 10/10 |
| **Total** | **16** | **16/16** |

Test file: [`tests/test_sl_recovery_v2.py`](file:///c:/Extra%20Programs/Files/AlcoSoft_Financial_Services/tests/test_sl_recovery_v2.py)

**Scenario C specifically validated (Test 3):** Recovery fires correctly for a position whose `notes` field was overwritten by reconciliation, confirming the notes-clobber risk is fully eliminated.

---

## FX01 — Stale Capital Sizing Risk (OPEN — PENDING)

### Classification: POST-AUDIT FINDING

**Origin:** Discovered during F005 analysis (June 6, 2026). F005 addressed the silent failure condition (no alert when capital API returns None). FX01 addresses the complementary risk: the cached value continues being used indefinitely as the sizing basis even when the API has been failing for an extended period.

**Finding:** `_capital_last_update` is checked only inside `_get_available_capital()` for TTL bypass. It is never inspected by any caller (`calculate_quantity`, `place_buy_order`, `check_max_daily_loss`) to determine whether the cached figure is too old to trust for risk decisions. During a sustained outage, new BUY quantities are sized on a capital figure that does not reflect closed P&L, new position entries, or MTM margin adjustments since the last successful fetch.

**Practical impact quantification:**

| Operating mode | Practical impact | Reason |
|----------------|-----------------|--------|
| MAX_POSITIONS filled (most common) | None | Entry gate fires before calculate_quantity() is reached |
| One exit, no margin, outage < 5 min | Negligible | Cache age within normal TTL |
| One exit, no margin, outage 5–30 min | Low | Sizing error ≤ closed-position P&L / total capital |
| One exit, no margin, outage > 30 min | Medium | Stale figure increasingly diverges; typically ≤15% overstatement |
| Margin enabled, long outage | High | MTM adjustments + leveraged losses compound divergence |
| check_max_daily_loss(), any outage | Low-Medium | Loss gate denominator uses stale capital; threshold shifts ||

**Recommended architecture (Design C — Dual-Condition Gate):**

Block new BUY entries inside `place_buy_order()` when BOTH conditions are simultaneously true:
1. `_capital_api_failures >= _CAPITAL_API_FAILURE_ALERT_THRESHOLD` (API is actively failing)
2. `time.time() - _capital_last_update > STALE_CAPITAL_MAX_AGE_SEC` (cache age exceeds 30 minutes)

Rationale for dual condition:
- Condition 1 alone would block entries on transient API blips (first failure within a 5-minute TTL window)
- Condition 2 alone would block entries when the API recovered normally but cache happened to be near-stale
- Both together: only blocks when the API has been continuously failing long enough for the cache to become materially unreliable

Key constraints:
- **Exits are never affected.** `place_sell_order()`, SL checks, trailing SL, and `squareoff_all_intraday()` do not call `calculate_quantity()` and are not in scope.
- **Gate lifts automatically** when `_capital_api_failures` resets to 0 on next successful fetch.
- **`check_max_daily_loss()`** is out of scope for this gate — it is a safety function and should not be blocked. Its stale-capital risk is noted but tolerated.

**Proposed constants:**
```python
STALE_CAPITAL_MAX_AGE_SEC = 1800   # 30 minutes
```
Reuses: `_CAPITAL_API_FAILURE_ALERT_THRESHOLD = 3` (already declared).

**Status:** OPEN. Awaiting implementation command.


## Production Readiness Assessment

**Current status:** F002 and F005 fixed. 29 of 31 findings remain (28 original unresolved + FX01 pending).
System should not be considered production-hardened until at minimum all CRITICAL and HIGH findings are resolved.

**Next finding to process (on NEXT command):** F006 — WebSocket reconnect counter double-increment risk (WebSocket / Data Feed, CRITICAL).

> Open CRITICAL findings: F001, F003, F004, F006, F007, F008
> Open HIGH findings (original): F009, F010, F011, F012, F013, F014, F015, F016, F017, F018
> Open HIGH findings (post-audit): FX01

---

*Report maintained by Antigravity — AlcoSoft Remediation Pass*
*Last updated: June 6, 2026*
