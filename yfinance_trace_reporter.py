#!/usr/bin/env python3
# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   yfinance_trace_reporter.py — Export & Report yfinance Traces
#
#   Called at system shutdown to dump all yfinance call traces
#   for debugging and diagnostics.
# ============================================================

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def export_all_traces(output_file: str = "data/yfinance_traces.json"):
    """Export all yfinance traces to JSON."""
    try:
        from core.yfinance_tracer import get_tracer
        
        tracer = get_tracer()
        traces = tracer.get_all_traces()
        
        output = {
            "export_timestamp": datetime.now().isoformat(),
            "total_calls": len(traces),
            "calls": traces,
        }
        
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        
        logger.info(f"📊 Exported {len(traces)} yfinance traces to {output_file}")
        return output_file
        
    except Exception as e:
        logger.error(f"Failed to export traces: {e}")
        return None


def print_traces_human_readable():
    """Print human-readable trace summary."""
    try:
        from core.yfinance_tracer import get_tracer
        
        tracer = get_tracer()
        tracer.print_summary()
        
    except Exception as e:
        logger.error(f"Failed to print traces: {e}")


def shutdown_hook():
    """Called at system shutdown to export traces."""
    logger.info("\n" + "="*80)
    logger.info("YFINANCE TRACE REPORT — SYSTEM SHUTDOWN")
    logger.info("="*80)
    
    print_traces_human_readable()
    export_all_traces()
    
    logger.info("="*80 + "\n")
