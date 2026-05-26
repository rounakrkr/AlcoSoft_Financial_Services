#!/usr/bin/env python3
"""
Diagnostic: Kotak Neo WebSocket + token resolution (same path as main.py).
"""

import os
import sys
import time
import logging

os.chdir(r"c:\Extra Programs\Files\AlcoSoft_Financial_Services")
sys.path.insert(0, os.getcwd())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from core.kotak_client import get_client
from core.data_fetcher import (
    purge_invalid_token_cache,
    resolve_instrument_tokens,
    start_live_feed,
    get_feed_stats,
    get_latest_tick,
)

print("=" * 70)
print("NEOAPI WEBSOCKET DIAGNOSTICS")
print("=" * 70)

print("\n1. Authenticating with Kotak Neo...")
get_client()
print("✅ Client obtained")

print("\n2. Token resolution (EQ scrips only)...")
purge_invalid_token_cache()
test_symbols = ["RELIANCE", "INFY", "TCS"]
tokens = resolve_instrument_tokens(test_symbols)
print(f"   Resolved: {tokens}")

if not tokens:
    print("\n❌ FATAL: 0 tokens — WebSocket cannot subscribe.")
    print("   Check errors above (pSymbolName / pTrdSymbol from Kotak CSV).")
    sys.exit(1)

print("\n3. Starting live feed (production path)...")
start_live_feed(test_symbols)

print("\n4. Waiting 30s for ticks...")
for i in range(30):
  stats = get_feed_stats()
  counts = stats.get("tick_counts", {})
  total = sum(counts.values())
  if total > 0:
    print(f"   [{i+1}s] ticks={total} | {counts}")
  time.sleep(1)

print("\n" + "=" * 70)
print("RESULTS")
print("=" * 70)
stats = get_feed_stats()
counts = stats.get("tick_counts", {})
total = sum(counts.values())
print(f"Total ticks: {total}")
print(f"Per symbol:  {counts}")

for sym in test_symbols:
    tick = get_latest_tick(sym)
    if tick:
        print(f"  ✅ {sym}: LTP=₹{tick['ltp']}")
    else:
        print(f"  ❌ {sym}: no tick")

if total > 0:
    print("\n✅ WebSocket working. Run: python main.py")
else:
    print("\n⚠️  No ticks — confirm market is open (9:15–15:30 IST).")
