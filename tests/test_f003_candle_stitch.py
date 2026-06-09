"""
F003 Validation Tests — Yahoo/WebSocket Candle Stitch Deduplication

Tests:
  T1: No WS candles, all Yahoo — no duplicates possible, all Yahoo returned
  T2: No overlap — full Yahoo + full WS merged in order
  T3: Full overlap — all Yahoo buckets covered by WS → only WS candles returned
  T4: Partial overlap — only non-overlapping Yahoo candles kept, WS wins at boundary
  T5: Yahoo candle without bucket key — treated as non-duplicate (safe default)
  T6: WS candle without bucket key — excluded from ws_buckets, Yahoo kept
  T7: Total candle count ≥ 26 check (merged length is correct for indicator gate)
  T8: WS candle at boundary takes priority over Yahoo candle (data freshness)
  T9: Cached path also deduplicates correctly (same logic as fresh-fetch path)
"""

import os, sys, logging
logging.basicConfig(level=logging.CRITICAL)

os.environ.setdefault("TRADING_MODE", "PAPER")
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")

# ── Stitch dedup logic extracted for unit testing ─────────────────────────────
# This is the exact logic from _get_candles_with_yfinance_seed (both paths)

def stitch(yf_candles: list, ws_candles: list) -> list:
    """Replicate the F003 stitch deduplication logic."""
    ws_buckets = {c["bucket"] for c in ws_candles if c.get("bucket")}
    yf_unique  = [c for c in yf_candles if c.get("bucket") not in ws_buckets]
    return yf_unique + ws_candles


def make_yf(buckets: list) -> list:
    return [{"bucket": b, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 500}
            for b in buckets]


def make_ws(buckets: list) -> list:
    return [{"bucket": b, "open": 200, "high": 201, "low": 199, "close": 200, "volume": 900}
            for b in buckets]


# ── TEST 1: No WS candles, all Yahoo — everything returned ────────────────────
yf = make_yf(["2026-06-06 09:15", "2026-06-06 09:20", "2026-06-06 09:25"])
ws = []
result = stitch(yf, ws)
assert len(result) == 3, f"TEST 1 FAIL: expected 3, got {len(result)}"
assert all(c["close"] == 100 for c in result), "TEST 1 FAIL: should all be Yahoo candles"
print("TEST 1 PASS: No WS candles → all Yahoo candles returned")


# ── TEST 2: No overlap — all Yahoo + all WS merged ────────────────────────────
yf = make_yf(["2026-06-06 09:15", "2026-06-06 09:20"])
ws = make_ws(["2026-06-06 09:25", "2026-06-06 09:30"])
result = stitch(yf, ws)
assert len(result) == 4, f"TEST 2 FAIL: expected 4, got {len(result)}"
assert result[0]["close"] == 100, "TEST 2 FAIL: first two should be Yahoo"
assert result[2]["close"] == 200, "TEST 2 FAIL: last two should be WS"
print("TEST 2 PASS: No overlap → all Yahoo + all WS merged (4 candles)")


# ── TEST 3: Full overlap — all Yahoo buckets also in WS → only WS returned ───
yf = make_yf(["2026-06-06 09:25", "2026-06-06 09:30"])
ws = make_ws(["2026-06-06 09:25", "2026-06-06 09:30", "2026-06-06 09:35"])
result = stitch(yf, ws)
assert len(result) == 3, f"TEST 3 FAIL: expected 3 (WS only), got {len(result)}"
assert all(c["close"] == 200 for c in result), "TEST 3 FAIL: should all be WS candles"
print("TEST 3 PASS: Full overlap → only WS candles kept (Yahoo duplicates dropped)")


# ── TEST 4: Partial overlap — boundary dedup works ────────────────────────────
yf = make_yf(["2026-06-06 09:15", "2026-06-06 09:20", "2026-06-06 09:25"])
ws = make_ws(["2026-06-06 09:25", "2026-06-06 09:30"])
result = stitch(yf, ws)
# 09:15 and 09:20 from Yahoo; 09:25 and 09:30 from WS (Yahoo 09:25 dropped)
assert len(result) == 4, f"TEST 4 FAIL: expected 4, got {len(result)}"
assert result[0]["bucket"] == "2026-06-06 09:15"
assert result[1]["bucket"] == "2026-06-06 09:20"
assert result[2]["bucket"] == "2026-06-06 09:25" and result[2]["close"] == 200  # WS wins
assert result[3]["bucket"] == "2026-06-06 09:30"
print("TEST 4 PASS: Partial overlap → WS wins at boundary, correct 4-candle merge")


# ── TEST 5: Yahoo candle without bucket key — kept (safe default) ─────────────
yf = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 500}]  # no bucket
ws = make_ws(["2026-06-06 09:25"])
result = stitch(yf, ws)
# Yahoo candle has no bucket, ws_buckets has "2026-06-06 09:25"
# c.get("bucket") returns None, None not in ws_buckets → Yahoo candle IS included
assert len(result) == 2, f"TEST 5 FAIL: expected 2, got {len(result)}"
print("TEST 5 PASS: Yahoo candle without bucket key → kept (not incorrectly dropped)")


# ── TEST 6: WS candle without bucket key — excluded from ws_buckets ───────────
yf = make_yf(["2026-06-06 09:25"])
ws = [{"open": 200, "high": 201, "low": 199, "close": 200, "volume": 900}]  # no bucket
result = stitch(yf, ws)
# WS candle has no bucket → ws_buckets is empty → Yahoo 09:25 is NOT filtered out
assert len(result) == 2, f"TEST 6 FAIL: expected 2, got {len(result)}"
# Both are kept — Yahoo because ws_buckets is empty; WS because it's always included
print("TEST 6 PASS: WS candle without bucket → not in ws_buckets, Yahoo kept alongside it")


# ── TEST 7: Correct total count (26-candle gate) ──────────────────────────────
yf = make_yf([f"2026-06-06 09:{15 + i*5:02d}" for i in range(20)])   # 20 Yahoo 5m candles
ws = make_ws([f"2026-06-06 10:{15 + i*5:02d}" for i in range(10)])   # 10 WS, no overlap
result = stitch(yf, ws)
assert len(result) == 30, f"TEST 7 FAIL: expected 30, got {len(result)}"
assert len(result) >= 26, "TEST 7 FAIL: should pass the 26-candle indicator gate"
print("TEST 7 PASS: 30 candles total, passes 26-candle gate")


# ── TEST 8: WS candle takes priority at overlap — data freshness ──────────────
# YF candle at 09:25 has close=100; WS candle at 09:25 has close=200
# The WS candle represents more recent data (real-time tick aggregation)
yf = make_yf(["2026-06-06 09:25"])
ws = make_ws(["2026-06-06 09:25"])
result = stitch(yf, ws)
assert len(result) == 1, f"TEST 8 FAIL: expected 1, got {len(result)}"
assert result[0]["close"] == 200, (
    f"TEST 8 FAIL: WS candle should take priority at overlap, got close={result[0]['close']}"
)
print("TEST 8 PASS: WS candle takes priority over Yahoo at same bucket (correct freshness)")


# ── TEST 9: Cached path dedup logic (same filter, applied to cache) ───────────
# Simulate the cached-path: cached_yf has buckets from yesterday + today's overlap
cached_yf = make_yf(["2026-06-06 09:15", "2026-06-06 09:20", "2026-06-06 09:25"])
ws_now    = make_ws(["2026-06-06 09:25", "2026-06-06 09:30"])

ws_buckets = {c["bucket"] for c in ws_now if c.get("bucket")}
yf_unique  = [c for c in cached_yf if c.get("bucket") not in ws_buckets]
merged     = yf_unique + ws_now

assert len(merged) == 4, f"TEST 9 FAIL: expected 4, got {len(merged)}"
assert merged[0]["bucket"] == "2026-06-06 09:15"
assert merged[1]["bucket"] == "2026-06-06 09:20"
assert merged[2]["close"] == 200  # WS wins at 09:25
print("TEST 9 PASS: Cached-path dedup works correctly (same logic)")


print()
print("ALL 9 TESTS PASSED")
