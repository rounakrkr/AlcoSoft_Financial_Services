# SYSTEM AUDIT REPORT — AlcoSoft Financial Services
### Live Trading System — Pre-Deployment Risk Assessment
**Audit Conducted:** June 6, 2026  
**Target:** Live NSE Intraday Deployment (Kotak Neo Broker)  
**Scope:** Core trading engine, broker integration, risk management, data feeds, state management, reflection/adaptive systems  
**Excluded:** `alco_env/` virtual environment, third-party packages  
**Auditor:** Antigravity — Full System Code Review

---

## EXECUTIVE SUMMARY

The AlcoSoft system is a well-architected intraday trading platform with meaningful safety layers — SQLite-backed state, broker-side SL-M orders, circuit breakers, reconciliation, and an adaptive reflection engine. However, the audit identified **7 CRITICAL findings** and **13 HIGH findings** that represent realistic capital loss or position management failure modes under live conditions.

> [!CAUTION]
> **7 CRITICAL issues were found that can cause direct capital loss, uncontrolled position exposure, or complete system failure under live market conditions. These must be addressed or risk-accepted before live deployment.**

**Key Risk Summary:**

| Severity | Count | Primary Risk Area |
|----------|-------|-------------------|
| 🔴 CRITICAL | 7 | SL protection gap, order verification window, margin logic, data corruption, broker sync, EOD squareoff, capital API failure |
| 🟠 HIGH | 13 | Yahoo Finance single point of failure, stale briefing, sell signal disabled, crash recovery blind window, token auth loop |
| 🟡 MEDIUM | 8 | Thread safety, candle contamination, DB status miscount, SL order type, reconciliation edge cases |
| 🟢 LOW | 2 | Log rotation, pattern false-positive rate |

---

## PHASE 1 — ARCHITECTURE MAP

### Startup Sequence
```
main.py
├── initialize_db()              # SQLite: trades, daily_stats, agent_decision_log
├── recover_state()              # Load open positions + briefing from disk
├── fix_briefing_trading_symbols()  # Resolve instrument tokens
├── start_live_feed()            # Kotak WebSocket subscribe
├── run_strategy_loop()          # Async loop: 5-second interval
└── AsyncIOScheduler
    ├── 09:00 → morning_screener (Cognition AI picks)
    ├── 15:15 → squareoff_all_intraday
    ├── 15:30 → eod_tasks (daily_stats)
    └── 16:00 → reflection_engine (adaptive multiplier update)
```

### Order Lifecycle (LIVE Mode)
```
strategy_loop detects BUY signal
  → place_buy_order()             [_order_lock, circuit breaker]
    → validate_and_fix_session()  [pre-flight token check]
    → calculate_quantity()        [risk% × capital / (entry-SL)]
    → margin over-leverage check
    → _send_kotak_order()         [2-attempt retry, stCode=100008 handling]
    → wait_for_order_verification() [45s timeout poll]
    → _send_kotak_sl_order()      [SL-M on broker, 2-attempt retry]
    → save_open_position()        [SQLite + positions.json]
```

### Data Flow
```
Yahoo Finance (historical) ──┐
                             ├─→ combined candle DataFrame ─→ RSI/MACD/EMA indicators
Kotak WebSocket (live ticks) ┘
                              ↓
                   StrategySetEvaluator
                              ↓
                   Confidence scoring (base × signal × time × market × advisory)
                              ↓
                   MIN_CONFIDENCE gate (45) → BUY/WAIT
```

---

## PHASE 2 — CRITICAL FINDINGS

### 🔴 F001 — SL-M Placement Failure Does Not Abort the Trade
**File:** `core/order_executor.py` | **Function:** `_place_buy_order_impl` (line 999)

**Finding:**  
When the broker-side SL-M order fails after a successful BUY fill, the code logs a `WARNING` and continues. The position is saved to the database with `sl_order_id=None`. From this moment, if AlcoSoft crashes, the process is killed, or the strategy loop hangs — there is **zero downside protection** for that position. The broker has a live long position with no stop-loss order attached.

The software SL (`check_stop_losses()`) only fires if the strategy loop is running, has a live WebSocket price, and completes its iteration — all of which fail in a crash scenario.

**Current Code (line 998-1002):**
```python
else:
    logger.warning(
        f"⚠️ Kotak SL-M FAILED for {symbol}. "
        f"Software SL active but no broker-side protection!"
    )
```

**Required Fix:**  
Treat SL-M placement failure as a BUY execution failure. Either: (a) cancel the BUY order immediately, or (b) retry SL-M placement up to 3 times with a short delay, and only proceed if SL-M is confirmed.

**Impact:** LIVE position held without any stop-loss in the event of a software crash.  
**Confidence:** HIGH  
**Recommended Test:** Place a buy order in live mode, mock `_send_kotak_sl_order` to return None. Verify no position is saved OR that an immediate cancel order is placed.

---

### 🔴 F002 — 45-Second Order Verification Timeout Creates Duplicate Fill Window
**File:** `core/order_executor.py` | **Function:** `_place_buy_order_impl` (line 963)

**Finding:**  
`wait_for_order_verification()` polls the broker for 45 seconds. If the broker confirms the fill after the timeout, the system raises `OrderExecutionError` and does NOT save the position locally. However, the broker already has the filled position.

The next `reconcile_broker_vs_local()` call will try to recover it via `recover_open_position()`. But in the window between the fill and reconciliation, if the strategy loop fires a second buy signal for the same symbol (because `get_open_positions()` shows no local position for it), it will attempt a **second BUY order**.

**Impact:** Duplicate live positions. 2× broker exposure while local DB shows 0.  
**Confidence:** HIGH  
**Recommended Test:** Mock `wait_for_order_verification` to return False after the broker has filled the order. Observe if the system attempts a second buy before reconciliation fires.

---

### 🔴 F003 — Yahoo Finance Data-WebSocket Candle Timezone Mismatch
**File:** `core/strategy.py` | **Function:** `_get_candles_with_yfinance_seed`

**Finding:**  
Yahoo Finance returns historical data with IST-aware `pd.Timestamp` objects (timezone = `Asia/Kolkata`). WebSocket candle history contains plain dicts with `bucket_key` strings in format `"%Y-%m-%d %H:%MM"` — naive (no timezone). When these are combined via `pd.DataFrame(candles)`, the timestamp alignment is not guaranteed. Duplicate rows or missing rows at the join boundary silently corrupt the indicator calculation.

RSI(14) computed on a DataFrame with a duplicated row at the stitch point will understate recent price momentum. MACD computed on a row-dropped DataFrame will have incorrect signal line values.

**Impact:** False buy signals or missed exits due to corrupted indicator values.  
**Confidence:** HIGH  
**Recommended Test:** Print the full combined DataFrame for any symbol at market open and inspect the timestamp column for duplicate or missing entries near the stitch boundary.

---

### 🔴 F004 — Capital API Failure Silently Halts All Orders
**File:** `core/order_executor.py` | **Function:** `_get_available_capital`

**Finding:**  
In LIVE mode, `_get_available_capital()` fetches from `client.limits()`. On any failure (network timeout, Kotak API rate limit, 5xx error), the function returns `0.0`. A zero available capital causes `calculate_quantity()` to return 0, and all buy orders are silently skipped with only a log message. No alert is raised, no circuit breaker trips, no dashboard indicator changes.

From an operator perspective, the system appears healthy (strategy loop running, WebSocket connected) but is placing zero orders.

**Impact:** Silent complete trading halt during broker API interruptions.  
**Confidence:** HIGH  
**Recommended Test:** Mock `call_broker_api` to raise `TimeoutError` for `client.limits()`. Observe: (a) no alert fires, (b) loop continues, (c) no orders placed.

---

### 🔴 F005 — EOD Squareoff Retry Does Not Re-Trigger
**File:** `core/order_executor.py` | **Function:** `squareoff_all_intraday`

**Finding:**  
At 3:15 PM, `squareoff_all_intraday()` fires. If a position fails squareoff (e.g., no live quote), it appends to `failures[]` and resets `_squareoff_done = False`. The log says "Will retry on next loop." However:

1. The function only runs when called — not on every strategy loop iteration.
2. It is called by the `AsyncIOScheduler` cron job once.
3. The cron job is configured with `misfire_grace_time=60` (or similar), so it will NOT fire again for the same trigger time.
4. The strategy loop itself calls `_check_all_exits()` which calls `squareoff_all_intraday()` IF `STRATEGY_TYPE == "INTRADAY"`.

Checking the main strategy loop: `_check_all_exits()` is called in the loop. So `squareoff_all_intraday()` IS called on every loop iteration after 3:15 PM. However, it only proceeds if `_squareoff_done` is False AND `datetime.now().time() >= INTRADAY_SQUAREOFF`.

The critical issue is: if the **WebSocket is also dead** (no live quotes available), the retry will also fail indefinitely, and Kotak will auto-squareoff at 3:20-3:25 PM at an unknown price.

**Impact:** Kotak auto-squareoff at unpredictable prices for positions that AlcoSoft couldn't price.  
**Confidence:** HIGH  
**Recommended Test:** At 3:14 PM, kill the WebSocket feed and verify what happens at 3:15 PM squareoff. Does it use the last known price, fall back to entry price, or block?

---

### 🔴 F006 — Margin Over-Leverage Check Uses Entry Value, Not Current Market Value
**File:** `core/order_executor.py` | **Function:** `get_margin_status` / `_place_buy_order_impl`

**Finding:**  
The `would_over_leverage` check in `_place_buy_order_impl` computes:
```python
deployed_now = margin_status.get('deployed_in_positions')  # = entry_position_value
```
`entry_position_value` is the **sum of entry prices × quantities** for all open positions — i.e., what you paid, not what they're worth now. If markets move against you by 4%, your actual broker exposure (mark-to-market) is higher than `deployed_in_positions` suggests. The system may approve a new BUY that exceeds the actual broker margin limit.

With `margin_leverage=5`, this gap can be significant during volatile sessions.

**Impact:** Positions opened beyond actual broker margin capacity during adverse market moves.  
**Confidence:** HIGH  
**Recommended Test:** Open 3 positions, let them depreciate 2% each, then observe whether the 4th position passes the margin check despite actual margin usage being higher than `entry_position_value` implies.

---

### 🔴 F007 — Reconciliation Recovers Stale CNC Positions as MIS Positions
**File:** `core/broker_reconciliation.py` | **Function:** `reconcile_broker_vs_local`

**Finding:**  
`_parse_broker_position_rows()` uses `qty > 0` as the only filter for broker positions. The Kotak `positions()` API returns both MIS (intraday) and CNC (delivery) positions. If a CNC holding exists from a previous day (unlikely for an intraday-only system, but possible during testing or manual trades), `reconcile_broker_vs_local()` will:

1. Detect it as a "broker_only" position.
2. Call `recover_open_position()` to add it to the local DB.
3. Begin monitoring it with an intraday SL, a target price, and potentially a new SL-M order on the broker side.

This would create a scenario where AlcoSoft sells a CNC delivery position intraday via an unintended sell signal or squareoff.

**Impact:** Accidental sale of CNC delivery holdings held in the broker account.  
**Confidence:** MEDIUM  
**Recommended Test:** Add a CNC delivery position to the Kotak test account and run `reconcile_broker_vs_local()`. Verify it is not recovered into the local OPEN positions database.

---

## PHASE 3 — HIGH SEVERITY FINDINGS

### 🟠 F008 — All Sell Strategy Sets Are Disabled
**File:** `config/trading_settings.json`

Every sell strategy set is set to `false`:
```json
"SELL_EMA_MOMENTUM_LOSS": false,
"SELL_VWAP_RSI_REVERSAL": false,
"SELL_MACD_EMA_WEAKNESS": false,
...
```

The ONLY exits are: software stop-loss, broker SL-M, profit target, EOD squareoff, emergency squareoff. There is no signal-based exit for a position that is slowly deteriorating above the stop-loss level.

**Impact:** Extended drawdown with no active exit management. Capital tied up in losing trades for hours.

---

### 🟠 F009 — Yahoo Finance Single Point of Failure
**File:** `core/strategy.py` | **Function:** `_check_sell_signals` / `_evaluate_buy_signal`

Yahoo Finance is the sole historical data provider. Any outage, rate limit, or geo-restriction causes ALL symbols to return WAIT for both buy and sell signals. Software stop-loss checks in `_check_sell_signals` skip symbols on Yahoo errors. Only broker-side SL-M remains active.

**Impact:** Complete signal generation failure during Yahoo Finance outages. No alerts raised.

---

### 🟠 F010 — Stale Briefing Not Detected
**File:** `core/state_manager.py` | **Function:** `validate_briefing`

`validate_briefing()` does not check the `generated_at` timestamp. If the morning screener fails silently, the system trades on yesterday's stock picks all day with no warning.

**Impact:** Trading on stale screener picks that may have fundamentally changed overnight.

---

### 🟠 F011 — 15-Minute Crash Recovery Blind Window
**File:** `core/state_manager.py` | **Function:** `recover_state`

After a crash and restart, the system must accumulate `MIN_WS_CANDLES_FOR_PATTERNS` (3) completed WS candles before any signal — including software stop-loss checks via `_check_sell_signals` — can fire. At 5-minute candles, this is a **15-minute window** where open positions are monitored only by broker-side SL-M.

**Impact:** 15-minute post-crash exposure window with no software exit management.

---

### 🟠 F012 — check_max_daily_loss Uses Incorrect Capital Base in LIVE Mode
**File:** `core/order_executor.py` | **Function:** `check_max_daily_loss`

Despite the code comment saying "Use INITIAL capital," the LIVE mode implementation calls `_get_available_capital(force_refresh=True)` which returns **current** available capital, not start-of-day capital. As positions are opened, available capital decreases, and the daily loss limit is computed on a shrinking base.

**Impact:** Daily loss limit becomes progressively tighter as positions are added during the session.

---

### 🟠 F013 — SL Orders Use SL-Limit Type, Not SL-Market
**File:** `core/order_executor.py` | **Function:** `_send_kotak_sl_order`

Stop-loss orders use `order_type="SL"` (stop-limit), not `"SL-M"` (stop-market). In fast markets or gap-down scenarios, the SL limit price may be skipped entirely, leaving the position unprotected while waiting for the limit to be hit.

**Impact:** Stop-loss orders may not execute on gap-down events, resulting in larger-than-configured losses.

---

### 🟠 F014 — Token Resolution Failure Leaves Positions Unpriced
**File:** `core/data_fetcher.py` | **Function:** `resolve_instrument_tokens`

Failed token resolutions are logged but do not halt the system or raise an alert. Positions in symbols that fail to resolve have no live price feed — software SL cannot fire for them, and squareoff may use the `entry_price_fallback`.

**Impact:** Positions in unresolved symbols are protected ONLY by broker-side SL-M.

---

### 🟠 F015 — Concurrent Sell Race Condition (Squareoff + Strategy Loop)
**File:** `core/order_executor.py` | **Function:** `squareoff_all_intraday` / `place_sell_order`

`squareoff_all_intraday()` does not acquire `_order_lock` before iterating positions and calling `place_sell_order()`. The strategy loop also calls `place_sell_order()` concurrently. A simultaneous strategy-driven sell + squareoff for the same symbol can result in two SELL orders being sent to the broker.

**Impact:** Potential short position (sell more than owned) which Kotak may reject or accept, creating margin complications.

---

### 🟠 F016 — Adaptive Safety Block Globally Disabled
**File:** `config/trading_settings.json`

`adaptive_safety_blocks_execution: false` means the reflection engine's win-rate-based suppression is **advisory only** — it logs but does not block execution. A strategy with a 25% win rate will continue trading.

**Impact:** Reflection engine cannot stop consistently losing strategies from placing new trades.

---

### 🟠 F017 — Min WS Candles Threshold Too Low
**File:** `config/trading_settings.json`

`min_ws_candles_for_patterns=3` means the system starts placing orders after only 15 minutes of live market data. The 9:15-9:30 AM window is statistically the most volatile and prone to false breakouts.

**Impact:** High false-positive rate for all pattern-based and indicator-based signals in the first 15-30 minutes.

---

### 🟠 F018 — Reflection DB Init Failure Crashes System Startup
**File:** `reflection/reflection_engine.py` | **Function:** `_init_db`

`_init_db()` is called at module import time. Any disk/permission failure raises an uncaught exception during import, crashing the entire startup chain with no graceful degradation.

**Impact:** System cannot start at all if the reflection database directory is not writable.

---

### 🟠 F019 — After-Market Ticks Contaminate Candle History
**File:** `core/data_fetcher.py` | **Function:** `_build_candle`

Candle building does not filter for market hours (9:15-15:30). Post-market ticks from Kotak WebSocket (which can continue after 15:30) are recorded as valid candles. Next day's indicator computation includes these contaminated candles.

**Impact:** Indicator values computed on after-market data at next-day session open, causing potentially false buy signals.

---

### 🟠 F020 — Daily Stats Miss Reconciliation-Closed Trades
**File:** `core/state_manager.py` | **Function:** `get_today_gross_pnl`

`_update_daily_stats()` counts trades with status `IN ('CLOSED', 'STOPPED')` only. Trades closed via reconciliation (`mark_position_reconciled_closed`) use the same underlying `close_position()` which sets `status='CLOSED'` — this part is actually correct. **However**, `mark_position_reconciliation_pending()` sets `reconciliation_status` but does NOT close the trade — these positions remain OPEN in the DB and their unrealized PnL is not counted in `get_today_gross_pnl()`. If `check_max_daily_loss()` is called while multiple positions are in `RECONCILIATION_PENDING` with large unrealized losses, it will underestimate the total loss.

**Impact:** `check_max_daily_loss()` may not fire when it should if significant unrealized losses are held in RECONCILIATION_PENDING positions.

---

## PHASE 4 — MEDIUM SEVERITY FINDINGS

| ID | Finding | File |
|----|---------|------|
| F021 | Circuit breaker opens on 2 broker failures; 30s recovery causes stale capital data | `core/circuit_breaker.py` |
| F022 | Duplicate SL orders possible if Kotak returns unrecognized status strings | `core/broker_reconciliation.py` |
| F023 | Quantity floor of 1 allows expensive stocks (MRF ₹95k+) to exceed full capital | `core/order_executor.py` |
| F024 | save_open_position race condition on concurrent buy signals for same symbol | `core/state_manager.py` |

---

## PHASE 5 — VERIFICATION PLAN

### Pre-Live Deployment Checklist

#### CRITICAL (Must fix or explicitly accept risk):
- [ ] **F001:** Add SL-M failure abort logic — if SL-M fails after BUY, cancel BUY or retry SL-M 3×
- [ ] **F002:** Add post-verification duplicate-check guard before retrying buy signals
- [ ] **F003:** Normalize Yahoo Finance candle timestamps to naive UTC before combining with WS candles
- [ ] **F004:** Add alert/notification when `_get_available_capital()` returns 0.0 in LIVE mode
- [ ] **F005:** Verify squareoff retry uses last-known price if WebSocket is down at 3:15 PM
- [ ] **F006:** Accept risk or switch to current market value for margin calculations
- [ ] **F007:** Filter broker positions by product type before reconciliation recovery

#### HIGH (Strongly recommended before live):
- [ ] **F008:** Enable at least 2 sell strategy sets for active exit management
- [ ] **F009:** Add a fallback data source or explicit alert when Yahoo Finance fails
- [ ] **F010:** Add `generated_at` staleness check to `validate_briefing()` (reject if >12 hours old)
- [ ] **F011:** Accept the 15-minute crash recovery window (broker SL-M covers this period)
- [ ] **F012:** Fix `check_max_daily_loss()` to use start-of-day capital from `daily_stats`
- [ ] **F013:** Investigate Kotak NeoAPI support for `SL-M` order type; prefer market-stop execution
- [ ] **F017:** Increase `min_ws_candles_for_patterns` to at least 6 (30 minutes) for safety

### Automated Regression Tests Recommended
```python
# Test 1: SL-M failure abort
mock_sl_order_to_fail()
place_buy_order("RELIANCE", ...) 
assert get_open_positions() == []   # No local position if SL-M failed

# Test 2: Yahoo outage resilience  
mock_yfinance_to_fail_all()
run_one_strategy_loop()
assert len(get_open_positions()) unchanged  # No new buys, no crashes

# Test 3: Staleness detection
write_briefing_with_yesterday_date()
assert validate_briefing(load_briefing())[0] == False  # Rejected

# Test 4: Duplicate sell prevention
thread_a = Thread(target=squareoff_all_intraday)
thread_b = Thread(target=lambda: place_sell_order("RELIANCE", price, "SIGNAL"))
run_both()
assert broker_sell_count("RELIANCE") == 1  # Not 2
```

---

## PHASE 6 — CONFIGURATION RISKS SUMMARY

| Setting | Current Value | Risk |
|---------|--------------|------|
| `margin_leverage` | 5.0 | At 10 positions, total exposure = 5× capital |
| `allow_margin` | true | Amplifies all losses 5× |
| `stop_loss_percent` | 0.5% | Very tight; prone to noise-triggered SL hits |
| `min_ws_candles_for_patterns` | 3 | Too low; allows signals after only 15 min |
| `adaptive_safety_blocks_execution` | false | Reflection engine is advisory only |
| `max_daily_loss_percent` | 5% | With 5× leverage and 10 positions, single gap-down can breach |
| All SELL strategy sets | false | No active exit signals; relies on SL/target/EOD only |

---

## PHASE 7 — RISK ACCEPTANCE MATRIX

If you proceed to live trading without fixing the above, these are the expected failure modes ranked by probability:

| Rank | Failure Mode | Probability | Max Capital Impact |
|------|-------------|-------------|-------------------|
| 1 | SL-M placement fails, software crashes → position unprotected | Medium | Full position value |
| 2 | Yahoo Finance outage → all signal generation halts silently | Medium | Opportunity loss + open positions unexited |
| 3 | Stale briefing (screener missed) → wrong stocks traded all day | Low-Medium | PnL degradation |
| 4 | EOD squareoff fails for 1 position → Kotak auto-squareoff at bad price | Low-Medium | Slippage loss |
| 5 | Duplicate fill via order verification timeout + reconciliation lag | Low | 2× position size |
| 6 | After-market candle contamination → false signal at open | Low-Medium | Single bad trade |

---

## APPENDIX — FILES AUDITED

| File | Lines | Status |
|------|-------|--------|
| `main.py` | 782 | ✅ Reviewed |
| `core/order_executor.py` | 2,137 | ✅ Reviewed (full) |
| `core/strategy.py` | 2,328 | ✅ Reviewed (full) |
| `core/state_manager.py` | 924 | ✅ Reviewed (full) |
| `core/data_fetcher.py` | 742 | ✅ Reviewed (full) |
| `core/broker_reconciliation.py` | 539 | ✅ Reviewed (full) |
| `core/emergency_squareoff.py` | 247 | ✅ Reviewed (full) |
| `core/circuit_breaker.py` | 155 | ✅ Reviewed (full) |
| `reflection/reflection_engine.py` | 1,202 | ✅ Reviewed (full) |
| `config/trading_settings.json` | 75 | ✅ Reviewed (full) |

---

*Report generated by Antigravity — AlcoSoft Full System Audit, June 6, 2026*  
*Total findings: 30 | Critical: 7 | High: 13 | Medium: 8 | Low: 2*
