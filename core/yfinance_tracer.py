# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/yfinance_tracer.py — Comprehensive yfinance Call Tracing
#   
#   REQUIREMENTS:
#   - Trace EVERY yfinance call
#   - Log: symbol, request start, request end, exception, traceback, metadata, retries
#   - NO fallbacks
#   - NO placeholder briefings
#   - Crash with full diagnostic on failure
# ============================================================

import time
import logging
import traceback
import sys
from datetime import datetime
from typing import Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#   TRACE STORAGE
# ════════════════════════════════════════════════════════════

class YFinanceTrace:
    """Immutable record of a single yfinance call."""
    
    def __init__(self):
        self.call_id = None
        self.symbol = None
        self.method = None              # 'download', 'Ticker.history', etc.
        self.params = {}                # period, interval, tickers, etc.
        
        self.request_start_time = None
        self.request_end_time = None
        self.elapsed_seconds = None
        
        self.success = None
        self.response_rows = None       # Number of rows returned
        self.response_columns = None    # List of columns
        self.response_shape = None      # (rows, cols)
        self.response_dtypes = None     # Column types
        
        self.exception_type = None
        self.exception_message = None
        self.exception_full_text = None
        self.traceback_lines = []
        
        # NEW: Detect specific error patterns
        self.is_json_error = False      # JSON parse error = no retry
        self.is_network_error = False   # Network error = may retry
        self.is_timeout_error = False   # Timeout = may retry
        
        self.retry_count = 0
        self.retry_attempts = []        # List of {"attempt": N, "success": bool, "error": str}
        
    def to_dict(self):
        return {
            "call_id": self.call_id,
            "symbol": self.symbol,
            "method": self.method,
            "params": self.params,
            "request_start": self.request_start_time.isoformat() if self.request_start_time else None,
            "request_end": self.request_end_time.isoformat() if self.request_end_time else None,
            "elapsed_seconds": self.elapsed_seconds,
            "success": self.success,
            "response_rows": self.response_rows,
            "response_columns": self.response_columns,
            "response_shape": self.response_shape,
            "response_dtypes": self.response_dtypes,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "traceback": self.traceback_lines,
            "retry_count": self.retry_count,
            "retry_attempts": self.retry_attempts,
            "error_classification": {
                "is_json_error": self.is_json_error,
                "is_network_error": self.is_network_error,
                "is_timeout_error": self.is_timeout_error,
            }
        }


class YFinanceTracer:
    """Global tracer for all yfinance calls."""
    
    def __init__(self):
        self._call_counter = 0
        self._traces = []               # All traces in order
        self._traces_by_symbol = defaultdict(list)
        self._current_call = None
        self._lock = None
        
    def start_call(self, symbol: str, method: str, params: dict) -> str:
        """Begin tracing a yfinance call. Returns call_id."""
        self._call_counter += 1
        call_id = f"yf_{self._call_counter}"
        
        trace = YFinanceTrace()
        trace.call_id = call_id
        trace.symbol = symbol
        trace.method = method
        trace.params = dict(params)  # snapshot
        trace.request_start_time = datetime.now()
        
        self._current_call = trace
        self._traces.append(trace)
        if symbol:
            self._traces_by_symbol[symbol].append(trace)
        
        logger.info(
            f"📡 [yfinance-START] {call_id} | {method} | symbol={symbol} | params={params}"
        )
        
        return call_id
    
    def end_call(self, call_id: str, response: Any = None, error: Exception = None):
        """End tracing a yfinance call."""
        if not self._current_call or self._current_call.call_id != call_id:
            logger.error(f"⚠️  [yfinance] Mismatched call_id: {call_id}")
            return
        
        trace = self._current_call
        trace.request_end_time = datetime.now()
        trace.elapsed_seconds = (trace.request_end_time - trace.request_start_time).total_seconds()
        
        if error:
            trace.success = False
            trace.exception_type = type(error).__name__
            trace.exception_message = str(error)
            trace.exception_full_text = repr(error)
            trace.traceback_lines = traceback.format_tb(sys.exc_info()[2])
            
            # Classify error for retry logic
            error_msg_lower = str(error).lower()
            if "json" in error_msg_lower or "expecting value" in error_msg_lower or "char 0" in error_msg_lower:
                trace.is_json_error = True
            elif "timeout" in error_msg_lower or "timed out" in error_msg_lower:
                trace.is_timeout_error = True
            elif "connection" in error_msg_lower or "network" in error_msg_lower or "http" in error_msg_lower:
                trace.is_network_error = True
            
            logger.error(
                f"❌ [yfinance-FAIL] {call_id} | {trace.method} | {trace.symbol} | "
                f"elapsed={trace.elapsed_seconds:.2f}s | {trace.exception_type}: {trace.exception_message}"
            )
            logger.error(f"   Traceback:\n{''.join(trace.traceback_lines)}")
            
        else:
            trace.success = True
            
            # Extract response metadata
            if response is not None:
                try:
                    if hasattr(response, 'shape'):
                        trace.response_shape = tuple(response.shape)
                        trace.response_rows = response.shape[0] if len(response.shape) > 0 else 0
                        trace.response_columns = list(response.columns) if hasattr(response, 'columns') else []
                    if hasattr(response, 'dtypes'):
                        trace.response_dtypes = {k: str(v) for k, v in response.dtypes.items()}
                except Exception as e:
                    logger.debug(f"Could not extract response metadata: {e}")
            
            logger.info(
                f"✅ [yfinance-OK] {call_id} | {trace.method} | {trace.symbol} | "
                f"elapsed={trace.elapsed_seconds:.2f}s | shape={trace.response_shape}"
            )
        
        self._current_call = None
    
    def record_retry_attempt(self, attempt_num: int, success: bool, error: Exception = None):
        """Record a retry attempt within the current call."""
        if not self._current_call:
            return
        
        self._current_call.retry_count = max(self._current_call.retry_count, attempt_num)
        
        error_msg = str(error) if error else None
        self._current_call.retry_attempts.append({
            "attempt": attempt_num,
            "success": success,
            "error": error_msg,
        })
        
        if error:
            logger.debug(
                f"  [retry] {self._current_call.call_id} attempt {attempt_num} failed: "
                f"{type(error).__name__}: {str(error)[:60]}"
            )
        else:
            logger.debug(f"  [retry] {self._current_call.call_id} attempt {attempt_num} succeeded")
    
    def get_all_traces(self) -> list[dict]:
        """Return all traces as dicts."""
        return [t.to_dict() for t in self._traces]
    
    def get_traces_for_symbol(self, symbol: str) -> list[dict]:
        """Return all traces for a specific symbol."""
        return [t.to_dict() for t in self._traces_by_symbol.get(symbol, [])]
    
    def print_summary(self):
        """Print human-readable summary of all calls."""
        logger.info("=" * 80)
        logger.info("YFINANCE TRACE SUMMARY")
        logger.info("=" * 80)
        
        total = len(self._traces)
        success = sum(1 for t in self._traces if t.success)
        failed = sum(1 for t in self._traces if not t.success)
        
        logger.info(f"Total calls: {total} | ✅ Success: {success} | ❌ Failed: {failed}")
        
        for symbol, traces in sorted(self._traces_by_symbol.items()):
            logger.info(f"\n{symbol}:")
            for trace in traces:
                status = "✅" if trace.success else "❌"
                logger.info(
                    f"  {status} {trace.call_id} | {trace.method} | "
                    f"{trace.elapsed_seconds:.2f}s | {trace.response_shape}"
                )
                if trace.exception_type:
                    logger.info(f"     → {trace.exception_type}: {trace.exception_message}")
                if trace.retry_count > 0:
                    logger.info(f"     → Retries: {trace.retry_count}")
        
        logger.info("=" * 80)


# ════════════════════════════════════════════════════════════
#   GLOBAL TRACER INSTANCE
# ════════════════════════════════════════════════════════════

_global_tracer = YFinanceTracer()


def get_tracer() -> YFinanceTracer:
    """Get the global yfinance tracer."""
    return _global_tracer


def should_retry_on_error(error: Exception) -> bool:
    """
    Determine if an error is retryable.
    
    Returns False for:
      - JSON parsing errors (means Yahoo has no data for symbol)
      - Similar errors that indicate no data exists
    
    Returns True for:
      - Network errors (may be transient)
      - Timeouts (may be transient)
    """
    error_msg_lower = str(error).lower()
    
    # JSON errors = not retryable (no data on Yahoo)
    if "json" in error_msg_lower or "expecting value" in error_msg_lower or "char 0" in error_msg_lower:
        return False
    
    # "No price data" = not retryable
    if "no price data" in error_msg_lower or "delisted" in error_msg_lower:
        return False
    
    # Everything else may be transient, so retry
    return True


# ════════════════════════════════════════════════════════════
#   WRAPPER FUNCTIONS — NO FALLBACKS, NO PLACEHOLDERS
# ════════════════════════════════════════════════════════════

def download_traced(
    tickers: str | list,
    period: str = "1d",
    interval: str = "1d",
    **kwargs
) -> Any:
    """
    Traced wrapper around yfinance.download().
    
    REQUIREMENT: CRASH if download fails. No fallbacks, no placeholders.
    """
    import yfinance as yf
    import pandas as pd
    
    if isinstance(tickers, list):
        tickers_str = ", ".join(tickers)
    else:
        tickers_str = str(tickers)
    
    # Start trace
    call_id = _global_tracer.start_call(
        symbol=tickers_str,
        method="download",
        params={
            "tickers": tickers_str,
            "period": period,
            "interval": interval,
            **kwargs
        }
    )
    
    try:
        # Execute the download
        result = yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            **kwargs
        )
        
        # End trace (success)
        _global_tracer.end_call(call_id, response=result)
        
        return result
        
    except Exception as e:
        # End trace (failure)
        _global_tracer.end_call(call_id, error=e)
        
        # REQUIREMENT: raise with full diagnostic so callers can safe-fail.
        logger.critical(
            f"\n"
            f"{'='*80}\n"
            f"YFINANCE DOWNLOAD FAILED - SAFE FAIL\n"
            f"{'='*80}\n"
            f"Call ID: {call_id}\n"
            f"Tickers: {tickers_str}\n"
            f"Period: {period}\n"
            f"Interval: {interval}\n"
            f"Exception Type: {type(e).__name__}\n"
            f"Exception Message: {str(e)}\n"
            f"Full Traceback:\n{''.join(traceback.format_tb(sys.exc_info()[2]))}\n"
            f"{'='*80}\n"
        )
        raise


def ticker_history_traced(
    symbol: str,
    period: str = "1d",
    interval: str = "1d",
    **kwargs
) -> Any:
    """
    Traced wrapper around yfinance.Ticker().history().
    
    REQUIREMENT: CRASH if history fetch fails. No fallbacks, no placeholders.
    """
    import yfinance as yf
    
    # Start trace
    call_id = _global_tracer.start_call(
        symbol=symbol,
        method="Ticker.history",
        params={
            "symbol": symbol,
            "period": period,
            "interval": interval,
            **kwargs
        }
    )
    
    try:
        # Execute the history fetch
        ticker = yf.Ticker(symbol)
        result = ticker.history(period=period, interval=interval, **kwargs)
        
        # End trace (success)
        _global_tracer.end_call(call_id, response=result)
        
        return result
        
    except Exception as e:
        # End trace (failure)
        _global_tracer.end_call(call_id, error=e)
        
        # REQUIREMENT: raise with full diagnostic so callers can safe-fail.
        logger.critical(
            f"\n"
            f"{'='*80}\n"
            f"YFINANCE TICKER.HISTORY FAILED - SAFE FAIL\n"
            f"{'='*80}\n"
            f"Call ID: {call_id}\n"
            f"Symbol: {symbol}\n"
            f"Period: {period}\n"
            f"Interval: {interval}\n"
            f"Exception Type: {type(e).__name__}\n"
            f"Exception Message: {str(e)}\n"
            f"Full Traceback:\n{''.join(traceback.format_tb(sys.exc_info()[2]))}\n"
            f"{'='*80}\n"
        )
        raise


def ticker_history_with_retry_traced(
    symbol: str,
    attempts: list[tuple[str, str]],
    **kwargs
) -> Any:
    """
    Traced wrapper for retry logic: attempt multiple (period, interval) combinations.
    
    attempts: [(period, interval), (period, interval), ...]
    
    REQUIREMENT: CRASH if ALL attempts fail. No fallbacks, no placeholders.
    """
    import yfinance as yf
    
    # Start main trace
    call_id = _global_tracer.start_call(
        symbol=symbol,
        method="Ticker.history_with_retry",
        params={
            "symbol": symbol,
            "attempts": attempts,
            **kwargs
        }
    )
    
    last_error = None
    
    for attempt_num, (period, interval) in enumerate(attempts, 1):
        try:
            _global_tracer.record_retry_attempt(attempt_num, success=False)  # Mark as pending
            
            # Execute attempt
            ticker = yf.Ticker(symbol)
            result = ticker.history(period=period, interval=interval, **kwargs)
            
            if not result.empty:
                # Success!
                _global_tracer.record_retry_attempt(attempt_num, success=True)
                _global_tracer.end_call(call_id, response=result)
                return result
            else:
                _global_tracer.record_retry_attempt(attempt_num, success=False, error=Exception("Empty result"))
                last_error = Exception(f"Empty result for period={period}, interval={interval}")
                
        except Exception as e:
            last_error = e
            _global_tracer.record_retry_attempt(attempt_num, success=False, error=e)
    
    # All attempts failed
    _global_tracer.end_call(call_id, error=last_error)
    
    # REQUIREMENT: raise with full diagnostic so callers can safe-fail.
    logger.critical(
        f"\n"
        f"{'='*80}\n"
        f"YFINANCE TICKER.HISTORY RETRY EXHAUSTED - SAFE FAIL\n"
        f"{'='*80}\n"
        f"Call ID: {call_id}\n"
        f"Symbol: {symbol}\n"
        f"Attempts: {attempts}\n"
        f"All {len(attempts)} attempts failed.\n"
        f"Last Error: {type(last_error).__name__}: {str(last_error)}\n"
        f"Full Traceback:\n{''.join(traceback.format_tb(sys.exc_info()[2]))}\n"
        f"{'='*80}\n"
    )
    raise last_error


# ════════════════════════════════════════════════════════════
#   DIAGNOSTIC EXPORT
# ════════════════════════════════════════════════════════════

def export_traces(filepath: str = "data/yfinance_traces.json"):
    """Export all traces to JSON for analysis."""
    import json
    
    traces = _global_tracer.get_all_traces()
    
    try:
        with open(filepath, 'w') as f:
            json.dump(traces, f, indent=2, default=str)
        logger.info(f"📊 Exported {len(traces)} yfinance traces to {filepath}")
    except Exception as e:
        logger.error(f"Failed to export traces: {e}")


def print_trace_summary():
    """Print human-readable summary."""
    _global_tracer.print_summary()
