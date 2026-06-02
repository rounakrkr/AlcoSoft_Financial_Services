#!/usr/bin/env python3
# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   test_yfinance_tracer.py — Test yfinance Tracer
#
#   Comprehensive test of yfinance tracing functionality.
#   Tests both success and failure scenarios.
# ============================================================

import sys
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

from core.yfinance_tracer import (
    download_traced,
    ticker_history_traced,
    ticker_history_with_retry_traced,
    get_tracer,
    export_traces,
    print_trace_summary,
)


def test_successful_download():
    """Test successful batch download."""
    logger.info("\n" + "="*80)
    logger.info("TEST 1: Successful batch download")
    logger.info("="*80)
    
    try:
        result = download_traced(
            tickers="INFY.NS TCS.NS",
            period="5d",
            interval="1d",
            progress=False,
        )
        logger.info(f"✅ Download succeeded: {result.shape}")
        return True
    except Exception as e:
        logger.error(f"❌ Download failed: {e}")
        return False


def test_successful_ticker_history():
    """Test successful ticker history fetch."""
    logger.info("\n" + "="*80)
    logger.info("TEST 2: Successful ticker history")
    logger.info("="*80)
    
    try:
        result = ticker_history_traced(
            symbol="INFY.NS",
            period="5d",
            interval="1d",
        )
        logger.info(f"✅ Ticker history succeeded: {result.shape}")
        return True
    except Exception as e:
        logger.error(f"❌ Ticker history failed: {e}")
        return False


def test_retry_mechanism():
    """Test retry mechanism."""
    logger.info("\n" + "="*80)
    logger.info("TEST 3: Retry mechanism (multiple attempts)")
    logger.info("="*80)
    
    try:
        result = ticker_history_with_retry_traced(
            symbol="INFY.NS",
            attempts=[
                ("5d", "5m"),
                ("1d", "1h"),
            ],
        )
        logger.info(f"✅ Retry mechanism succeeded: {result.shape}")
        return True
    except Exception as e:
        logger.error(f"❌ Retry mechanism failed: {e}")
        return False


def test_invalid_symbol():
    """Test with invalid symbol (should crash with diagnostic)."""
    logger.info("\n" + "="*80)
    logger.info("TEST 4: Invalid symbol (should crash with diagnostic)")
    logger.info("="*80)
    
    try:
        result = download_traced(
            tickers="INVALID_SYMBOL_XYZ.NS",
            period="5d",
            interval="1d",
            progress=False,
        )
        logger.error(f"❌ Should have crashed for invalid symbol")
        return False
    except Exception as e:
        logger.info(f"✅ Correctly crashed with: {type(e).__name__}")
        return True


def main():
    """Run all tests."""
    logger.info("\n")
    logger.info("╔" + "="*78 + "╗")
    logger.info("║" + " "*15 + "YFINANCE TRACER COMPREHENSIVE TEST" + " "*30 + "║")
    logger.info("╚" + "="*78 + "╝")
    
    results = {
        "Successful Download": test_successful_download(),
        "Successful Ticker History": test_successful_ticker_history(),
        "Retry Mechanism": test_retry_mechanism(),
        "Invalid Symbol Handling": test_invalid_symbol(),
    }
    
    # Print summary
    logger.info("\n" + "="*80)
    logger.info("TEST RESULTS")
    logger.info("="*80)
    
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"{status} | {test_name}")
    
    # Print trace summary
    print_trace_summary()
    
    # Export traces
    export_traces()
    
    # Print full traces
    tracer = get_tracer()
    traces = tracer.get_all_traces()
    
    logger.info("\n" + "="*80)
    logger.info("FULL TRACE DETAILS")
    logger.info("="*80)
    
    for trace in traces:
        logger.info(f"\nCall ID: {trace['call_id']}")
        logger.info(f"  Method: {trace['method']}")
        logger.info(f"  Symbol: {trace['symbol']}")
        logger.info(f"  Params: {trace['params']}")
        logger.info(f"  Start: {trace['request_start']}")
        logger.info(f"  End: {trace['request_end']}")
        logger.info(f"  Elapsed: {trace['elapsed_seconds']:.2f}s")
        logger.info(f"  Success: {trace['success']}")
        logger.info(f"  Shape: {trace['response_shape']}")
        if trace['exception_type']:
            logger.info(f"  Exception: {trace['exception_type']}: {trace['exception_message']}")
            logger.info(f"  Traceback:\n{''.join(trace['traceback'])}")
        if trace['retry_count'] > 0:
            logger.info(f"  Retries: {trace['retry_count']}")
            for attempt in trace['retry_attempts']:
                logger.info(f"    {attempt}")
    
    # Summary
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed
    
    logger.info("\n" + "="*80)
    logger.info(f"SUMMARY: {passed}/{total} tests passed, {failed} failed")
    logger.info("="*80 + "\n")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
