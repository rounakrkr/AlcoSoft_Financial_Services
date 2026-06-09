import asyncio
import os
import sys
import time
import json
from datetime import datetime, timedelta

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import strategy
from core.state_manager import BRIEFING_PATH, atomic_write_json
import logging
logging.basicConfig(level=logging.INFO)

def write_test_briefing(date_str, approved, watchlist):
    data = {
        "session_type": "MORNING_SCREENER",
        "generated_at": date_str,
        "market_bias": "BULLISH",
        "approved_stocks": approved,
        "watchlist": watchlist,
        "avoid_list": []
    }
    with open(BRIEFING_PATH, "w") as f:
        json.dump(data, f)
    # Force strategy to reload by resetting cache time
    strategy._briefing_cache_time = 0.0

def run_tests():
    print("--- F018 FUNCTIONAL TEST ---")

    # SCENARIO A: Fresh Briefing
    print("\n[SCENARIO A] Fresh Briefing")
    today_str = datetime.now().isoformat()
    write_test_briefing(today_str, [{"ticker": "RELIANCE"}], [{"ticker": "TCS"}])
    briefing_A = strategy._get_briefing_cached()
    print(f"Result: {briefing_A['session_type']}")
    print(f"Approved: {[s['ticker'] for s in briefing_A.get('approved_stocks', [])]}")
    print(f"Watchlist: {[s['ticker'] for s in briefing_A.get('watchlist', [])]}")

    # SCENARIO B: Stale Briefing
    print("\n[SCENARIO B] Stale Briefing")
    yesterday_str = (datetime.now() - timedelta(days=1)).isoformat()
    write_test_briefing(yesterday_str, [{"ticker": "HDFCBANK"}], [{"ticker": "INFY"}])
    briefing_B = strategy._get_briefing_cached()
    print(f"Result: {briefing_B['session_type']}")
    print(f"Approved: {briefing_B.get('approved_stocks', [])}")
    print(f"Watchlist: {briefing_B.get('watchlist', [])}")

    # SCENARIO C: Auto Recovery
    print("\n[SCENARIO C] Auto Recovery")
    print("Writing fresh briefing again...")
    write_test_briefing(today_str, [{"ticker": "ITC"}], [{"ticker": "SBIN"}])
    briefing_C = strategy._get_briefing_cached()
    print(f"Result: {briefing_C['session_type']}")
    print(f"Approved: {[s['ticker'] for s in briefing_C.get('approved_stocks', [])]}")
    print(f"Watchlist: {[s['ticker'] for s in briefing_C.get('watchlist', [])]}")

    print("\n✅ All scenarios executed.")

if __name__ == "__main__":
    run_tests()
