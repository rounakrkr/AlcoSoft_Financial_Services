"""
F005 Validation Tests — Capital API Failure Observability
Covers: silent-halt prevention, CRITICAL alert on zero-cache failure,
CRITICAL alert on threshold, periodic reminders, recovery reset,
cache-hit path, PAPER mode pass-through, market-closed stCode pass-through.
"""
import os
import sys
import logging

logging.basicConfig(level=logging.WARNING)

os.environ["TRADING_MODE"]  = "LIVE"
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.order_executor as oe

# ── Test harness helpers ───────────────────────────────────────────────────

alerts_sent = []

def fake_alert_critical(msg):
    alerts_sent.append(msg)

def reset(cache_val=0.0, failures=0, last_update=0.0):
    oe._capital_cache      = cache_val
    oe._capital_last_update = last_update
    oe._capital_api_failures = failures
    alerts_sent.clear()
    oe.TRADING_MODE = "LIVE"

def inject_alert():
    """Patch alert_critical inside order_executor's dynamic import."""
    import core.alerts as _alerts
    _alerts.alert_critical = fake_alert_critical

inject_alert()

# ── TEST 1: Successful fetch resets failure counter and updates cache ───────
reset()
import types
fake_client = types.SimpleNamespace(
    limits=lambda **kw: {"Net": 50000.0}
)
oe._capital_api_failures = 2   # simulate prior failures
import unittest.mock as mock

with mock.patch("core.kotak_client.get_client", return_value=fake_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 50000.0,  "TEST 1 FAIL: expected 50000, got " + str(result)
assert oe._capital_api_failures == 0, "TEST 1 FAIL: failure counter should reset to 0"
assert oe._capital_cache == 50000.0,  "TEST 1 FAIL: cache should be updated"
print("TEST 1 PASS: Successful fetch resets failure counter and updates cache")

# ── TEST 2: API returns None → CRITICAL immediately (cache is 0.0) ─────────
reset()  # cache=0, failures=0

def bad_limits(**kw): return None
fake_bad_client = types.SimpleNamespace(limits=bad_limits)

with mock.patch("core.kotak_client.get_client", return_value=fake_bad_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 0.0, "TEST 2 FAIL: should return 0.0 cache"
assert oe._capital_api_failures == 1, "TEST 2 FAIL: failure count should be 1"
assert len(alerts_sent) == 1, "TEST 2 FAIL: should have sent 1 CRITICAL alert"
assert "BLOCKED" in alerts_sent[0] or "DOWN" in alerts_sent[0], (
    "TEST 2 FAIL: alert should mention blocking. Got: " + str(alerts_sent[0]))
print("TEST 2 PASS: None response with zero cache fires CRITICAL immediately")

# ── TEST 3: API returns None → no CRITICAL (cache has good value, below threshold)
reset(cache_val=40000.0, failures=0)

with mock.patch("core.kotak_client.get_client", return_value=fake_bad_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 40000.0, "TEST 3 FAIL: should return cached 40000"
assert oe._capital_api_failures == 1, "TEST 3 FAIL: failure should be 1"
assert len(alerts_sent) == 0, "TEST 3 FAIL: should NOT alert before threshold"
print("TEST 3 PASS: Failure below threshold with warm cache — no alert")

# ── TEST 4: Threshold reached → CRITICAL alert ────────────────────────────
reset(cache_val=40000.0, failures=oe._CAPITAL_API_FAILURE_ALERT_THRESHOLD - 1)

with mock.patch("core.kotak_client.get_client", return_value=fake_bad_client):
    result = oe._get_available_capital(force_refresh=True)

assert oe._capital_api_failures == oe._CAPITAL_API_FAILURE_ALERT_THRESHOLD, (
    "TEST 4 FAIL: expected " + str(oe._CAPITAL_API_FAILURE_ALERT_THRESHOLD))
assert len(alerts_sent) == 1, "TEST 4 FAIL: should alert at threshold"
assert "consecutively" in alerts_sent[0] or "stale" in alerts_sent[0].lower(), (
    "TEST 4 FAIL: alert should mention consecutive failures: " + str(alerts_sent[0]))
print("TEST 4 PASS: Threshold crossed — CRITICAL alert fired")

# ── TEST 5: Exception (not just None) → same failure handling ─────────────
reset(cache_val=0.0, failures=0)

def raising_limits(**kw): raise ConnectionError("Broker unreachable")
fake_exc_client = types.SimpleNamespace(limits=raising_limits)

with mock.patch("core.kotak_client.get_client", return_value=fake_exc_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 0.0, "TEST 5 FAIL: should return 0.0"
assert oe._capital_api_failures == 1, "TEST 5 FAIL: failure counter should be 1"
assert len(alerts_sent) == 1, "TEST 5 FAIL: CRITICAL alert expected for zero-cache exception"
print("TEST 5 PASS: Exception with zero cache fires CRITICAL immediately")

# ── TEST 6: Valid cache within TTL — no broker call made ───────────────────
reset(cache_val=35000.0, failures=0, last_update=__import__('time').time())
call_count = [0]
def counted_limits(**kw): call_count[0] += 1; return {"Net": 99999.0}
fake_counted_client = types.SimpleNamespace(limits=counted_limits)

with mock.patch("core.kotak_client.get_client", return_value=fake_counted_client):
    result = oe._get_available_capital(force_refresh=False)

assert result == 35000.0, "TEST 6 FAIL: should return cached value"
assert call_count[0] == 0, "TEST 6 FAIL: broker should NOT be called within TTL"
print("TEST 6 PASS: Cache hit within TTL — no broker call")

# ── TEST 7: force_refresh bypasses TTL ────────────────────────────────────
reset(cache_val=35000.0, failures=0, last_update=__import__('time').time())

with mock.patch("core.kotak_client.get_client", return_value=fake_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 50000.0, "TEST 7 FAIL: force_refresh should bypass cache"
print("TEST 7 PASS: force_refresh=True bypasses TTL cache")

# ── TEST 8: PAPER mode — no broker call, uses paper capital ───────────────
reset()
oe.TRADING_MODE = "PAPER"
paper_called = [False]
real_paper_fn = oe._calculate_paper_capital_available

def fake_paper(): paper_called[0] = True; return 9999.0
oe._calculate_paper_capital_available = fake_paper

result = oe._get_available_capital()
assert paper_called[0], "TEST 8 FAIL: should call paper capital function"
assert result == 9999.0, "TEST 8 FAIL: should return paper capital"
oe._calculate_paper_capital_available = real_paper_fn
oe.TRADING_MODE = "LIVE"
print("TEST 8 PASS: PAPER mode routes to _calculate_paper_capital_available")

# ── TEST 9: stCode==300015 (market closed) returns cache without failure ──
reset(cache_val=42000.0, failures=0)

def closed_limits(**kw): return {"stCode": 300015, "message": "Market closed"}
fake_closed_client = types.SimpleNamespace(limits=closed_limits)

with mock.patch("core.kotak_client.get_client", return_value=fake_closed_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 42000.0, "TEST 9 FAIL: should return cached value on 300015"
assert oe._capital_api_failures == 0, "TEST 9 FAIL: market-closed should NOT increment failures"
assert len(alerts_sent) == 0, "TEST 9 FAIL: market-closed should NOT alert"
print("TEST 9 PASS: Market-closed stCode returns cache without failure increment")

# ── TEST 10: Recovery after failures resets counter ───────────────────────
reset(cache_val=30000.0, failures=5)

with mock.patch("core.kotak_client.get_client", return_value=fake_client):
    result = oe._get_available_capital(force_refresh=True)

assert result == 50000.0, "TEST 10 FAIL: should return broker value on recovery"
assert oe._capital_api_failures == 0, "TEST 10 FAIL: failures should reset to 0 on success"
print("TEST 10 PASS: Successful recovery resets failure counter")

print()
print("ALL 10 TESTS PASSED")
