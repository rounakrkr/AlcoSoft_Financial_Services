# YFINANCE COMPREHENSIVE TRACING SYSTEM

**Date**: 2026-06-02  
**Purpose**: Trace every yfinance call with full diagnostics. Safe-fail to WAIT on failure; no secondary market-data provider.

---

## REQUIREMENTS MET

✅ **Trace every yfinance call**
- Symbol
- Request start time
- Request end time
- Full traceback
- Response metadata (shape, columns, dtypes)
- Retry attempts tracking

✅ **Do NOT add secondary data providers**
- All yfinance failures are surfaced in logs and the symbol is marked WAIT
- No silent degradation to empty DataFrames
- No placeholder briefings

✅ **Do NOT continue silently**
- Every failure logs CRITICAL level
- Full diagnostic output included
- Thread timeouts treated as failures
- Affected symbols wait instead of continuing with bad data

✅ **Safe WAIT with full diagnostic output**
- Call ID
- Symbol(s) requested
- Parameters sent
- Raw exception type
- Exception message
- Full Python traceback
- Elapsed time
- Retry history (if any)

---

## FILES CREATED / MODIFIED

### 1. **core/yfinance_tracer.py** (NEW)
   - `YFinanceTrace` class: Immutable record of each call
   - `YFinanceTracer` class: Global tracer managing all calls
   - `download_traced()`: Wrapped yfinance.download()
   - `ticker_history_traced()`: Wrapped yfinance.Ticker().history()
   - `ticker_history_with_retry_traced()`: Wrapped with retry logic
   - `export_traces()`: Dump traces to JSON
   - `print_trace_summary()`: Human-readable summary

### 2. **core/strategy.py** (MODIFIED)
   - Import: `from core.yfinance_tracer import ticker_history_with_retry_traced`
   - `_fetch_yfinance_with_retry()`: Uses yfinance only
   - `_get_candles_with_yfinance_seed()`: Marks symbols WAIT when yfinance is unavailable
   - Exception handling: yfinance failures are logged without placing trades

### 3. **screener/morning_screener.py** (MODIFIED)
   - Import: `from core.yfinance_tracer import download_traced, ticker_history_traced`
   - `_fetch_yahoo_batch()`: Uses `download_traced()`, raises instead of returning empty
   - `_get_market_bias()`: Uses `ticker_history_traced()`, raises on timeout/error
   - `_get_stock_market_bias()`: Uses `ticker_history_traced()`, raises on timeout/error
   - yfinance failures are logged and affected symbols wait safely

### 4. **main.py** (MODIFIED)
   - Import: `from yfinance_trace_reporter import shutdown_hook as yfinance_shutdown_hook`
   - `_cleanup()`: Calls `yfinance_shutdown_hook()` before logout
   - On shutdown: Exports all traces to JSON and prints summary

### 5. **yfinance_trace_reporter.py** (NEW)
   - `export_all_traces()`: Dump traces to `data/yfinance_traces.json`
   - `print_traces_human_readable()`: Print summary to logs
   - `shutdown_hook()`: Called at system shutdown

### 6. **test_yfinance_tracer.py** (NEW)
   - Comprehensive test suite
   - Tests success and failure scenarios
   - Validates trace recording

---

## TRACE STRUCTURE

Each trace record (JSON format):
```json
{
  "call_id": "yf_1",
  "symbol": "INFY.NS",
  "method": "Ticker.history",
  "params": {
    "symbol": "INFY.NS",
    "period": "5d",
    "interval": "5m"
  },
  "request_start": "2026-06-02T10:30:45.123456",
  "request_end": "2026-06-02T10:30:48.456789",
  "elapsed_seconds": 3.333,
  "success": true,
  "response_rows": 50,
  "response_columns": ["Open", "High", "Low", "Close", "Volume"],
  "response_shape": [50, 5],
  "response_dtypes": {
    "Open": "float64",
    "High": "float64",
    "Low": "float64",
    "Close": "float64",
    "Volume": "float64"
  },
  "exception_type": null,
  "exception_message": null,
  "traceback": [],
  "retry_count": 0,
  "retry_attempts": []
}
```

---

## CALL FLOW (With Tracing)

### Success Path
```
screener.morning_screener._fetch_yahoo_batch()
  ↓
download_traced()
  ├─ tracer.start_call() → logs symbol, params, request_start
  ├─ yf.download() → executes
  ├─ tracer.end_call() → logs response_shape, elapsed_seconds
  └─ return DataFrame
```

### Failure Path
```
screener.morning_screener._fetch_yahoo_batch()
  ↓
download_traced()
  ├─ tracer.start_call() → logs symbol, params
  ├─ yf.download() → raises Exception
  ├─ tracer.end_call(error=e) → logs exception_type, message, full traceback
  ├─ logger.critical() → logs full diagnostic
  └─ raise → propagates to _fetch_yahoo_batch()
  
_fetch_yahoo_batch() catches in thread
  └─ raises RuntimeError("Yahoo Finance batch download failed: ...")
  
run_morning_screener()
  └─ yfinance error is logged → affected symbol waits
```

---

## LOGGING OUTPUT

### Startup (Trace Start)
```
📡 [yfinance-START] yf_5 | download | symbol=INFY.NS TCS.NS | params={'tickers': '...', 'period': '30d', ...}
```

### Success (Trace End)
```
✅ [yfinance-OK] yf_5 | download | INFY.NS TCS.NS | elapsed=2.45s | shape=(30, 10)
```

### Failure (Trace End + Critical)
```
❌ [yfinance-FAIL] yf_6 | Ticker.history | BADTICKER.NS | elapsed=1.23s | HTTPError: 404 Not Found
   Traceback:
   ...

YFINANCE DOWNLOAD FAILED - SAFE FAIL
...
============================================================
Call ID: yf_6
Symbol: BADTICKER.NS
Period: 5d
Interval: 5m
Exception Type: HTTPError
Exception Message: 404 Not Found
Full Traceback:
...
============================================================
```

---

## SHUTDOWN REPORT

When system shuts down, exports all traces:
```
================================================================================
YFINANCE TRACE REPORT — SYSTEM SHUTDOWN
================================================================================

================================================================================
YFINANCE TRACE SUMMARY
================================================================================
Total calls: 12 | ✅ Success: 11 | ❌ Failed: 1

INFY.NS:
  ✅ yf_1 | Ticker.history | 2.34s | (50, 5)
  ✅ yf_5 | download | 3.12s | (30, 10)

TCS.NS:
  ✅ yf_2 | Ticker.history | 2.18s | (45, 5)
  ❌ yf_8 | Ticker.history | 0.95s | HTTPError: 404 Not Found
     → Retries: 2

================================================================================

📊 Exported 12 yfinance traces to data/yfinance_traces.json
```

---

## TEST YFINANCE TRACER

Run the test suite:
```bash
python test_yfinance_tracer.py
```

Tests:
1. ✅ Successful batch download
2. ✅ Successful ticker history
3. ✅ Retry mechanism (multiple attempts)
4. ✅ Invalid symbol handling (safe WAIT with diagnostic)

---

## KEY CHANGES FROM PREVIOUS

### Before
- Try/except swallowed exceptions → system continued with empty DataFrames
- Silent WebSocket-only continuation
- Minimal logging of what happened
- System kept running even with broken data

### After
- yfinance errors surface clearly → affected symbol waits safely
- Every call logged with call_id for tracing
- Full traceback captured
- Retry attempts tracked separately
- Response metadata captured (shape, dtypes, columns)
- All traces exported to JSON on shutdown
- System fails fast instead of silently degrading

---

## DEBUGGING A FAILURE

1. **Check logs**: grep for `[yfinance-FAIL]` or `CRITICAL`
2. **Check JSON export**: `data/yfinance_traces.json`
3. **Find call_id**: e.g. `yf_5`
4. **Review trace record**:
   - What symbol? (symbol field)
   - What params? (params field)
   - What error? (exception_type, exception_message)
   - How long? (elapsed_seconds)
   - Did it retry? (retry_attempts field)
5. **Full traceback**: In the trace record or in `traceback` field

---

## REQUIREMENTS COMPLIANCE CHECKLIST

✅ **For each request show:**
- [x] Symbol → `trace["symbol"]`
- [x] Request start → `trace["request_start"]`
- [x] Request end → `trace["request_end"]`
- [x] Raw exception → `trace["exception_type"]` / `trace["exception_message"]`
- [x] Full traceback → `trace["traceback"]` (list of lines)
- [x] Response metadata → `trace["response_shape"]`, `trace["response_columns"]`, `trace["response_dtypes"]`
- [x] Retry attempts → `trace["retry_attempts"]` (list of dicts)

✅ **Do NOT add secondary data providers**
- Removed try/except blocks that swallowed errors silently
- No more empty DataFrame returns
- No alternate market-data provider
- System marks the affected symbol WAIT on yfinance failure

✅ **Do NOT create placeholder briefings**
- Screener raises exception instead of creating placeholder
- System doesn't start trading with bad data

✅ **Do NOT continue silently**
- Every yfinance call is logged (START/OK/FAIL)
- Timeouts are treated as exceptions
- Affected symbol waits immediately on failure

✅ **If yfinance fails: safe WAIT with full diagnostic output**
- CRITICAL level logging
- Exception type + message included
- Full Python traceback included
- Call parameters included
- Call ID for tracing
- System exits

---

## USAGE IN CODE

### Within strategy loop (core/strategy.py):
```python
from core.yfinance_tracer import ticker_history_with_retry_traced

hist = ticker_history_with_retry_traced(
    symbol="INFY.NS",
    attempts=[("5d", "5m"), ("1d", "1h")]
)
# If both attempts fail -> raises with full diagnostic for safe WAIT handling
# If either succeeds → returns DataFrame
```

### Within screener (screener/morning_screener.py):
```python
from core.yfinance_tracer import download_traced, ticker_history_traced

# Batch download
data = download_traced(
    tickers="INFY.NS TCS.NS",
    period="30d",
    interval="1d",
    progress=False,
)
# If fails -> raises with full diagnostic for safe WAIT handling

# Single ticker
hist = ticker_history_traced(
    symbol="^NSEI",
    period="5d",
    interval="1d",
)
# If fails -> raises with full diagnostic for safe WAIT handling
```

### At shutdown (main.py):
```python
from yfinance_trace_reporter import shutdown_hook

shutdown_hook()
# Prints summary to logs
# Exports all traces to data/yfinance_traces.json
```

---

## NEXT STEPS

The system is now ready for production with comprehensive yfinance tracing:

1. **Run system**: `python main.py`
2. **Monitor logs**: Watch for `[yfinance-START]`, `[yfinance-OK]`, `[yfinance-FAIL]`
3. **On yfinance failure**: Check logs for `CRITICAL` level yfinance errors
4. **On shutdown**: `data/yfinance_traces.json` contains full trace history
5. **Debug**: Use call_id to correlate traces with log entries
