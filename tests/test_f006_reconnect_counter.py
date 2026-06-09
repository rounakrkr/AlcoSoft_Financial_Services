"""
F006 Validation Tests — WebSocket Reconnect Counter Integrity
Covers:
  - Counter correctly increments and is not reset mid-sequence by start_live_feed
  - Feed-death alert is reachable after max_reconnect consecutive failures
  - Successful reconnect resets counter
  - Initial startup subscribe failure does NOT blow up startup() (schedule_reconnect called)
  - Counter is not double-incremented on a single failure
"""
import os
import sys
import threading
import logging

logging.basicConfig(level=logging.CRITICAL)  # suppress noise, only assertions matter

os.environ.setdefault("TRADING_MODE", "PAPER")
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.data_fetcher as df

# ── Harness helpers ────────────────────────────────────────────────────────────

alerts_sent = []
scheduled_timers = []

def fake_alert_critical(msg):
    alerts_sent.append(msg)

# Patch alert
import unittest.mock as mock
import core.alerts as _alerts
_alerts.alert_critical = fake_alert_critical


def reset_module():
    df._reconnect_attempts = 0
    df._subscribed_symbols = ["RELIANCE"]
    df._reconnect_timer = None
    df._active_client = None
    alerts_sent.clear()
    scheduled_timers.clear()


def cancel_pending_timer():
    """Cancel any live timer to avoid interference between tests."""
    if df._reconnect_timer:
        df._reconnect_timer.cancel()
        df._reconnect_timer = None


# ── TEST 1: start_live_feed(_is_reconnect=False) resets counter ────────────────
reset_module()
df._reconnect_attempts = 5   # simulate prior state

fake_client = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": mock.Mock(),
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._reset_keepalive"):
    df.start_live_feed(["RELIANCE"], _is_reconnect=False)

assert df._reconnect_attempts == 0, f"TEST 1 FAIL: expected 0, got {df._reconnect_attempts}"
cancel_pending_timer()
print("TEST 1 PASS: start_live_feed(_is_reconnect=False) resets counter")


# ── TEST 2: start_live_feed(_is_reconnect=True) does NOT reset counter ─────────
reset_module()
df._reconnect_attempts = 3   # simulate mid-reconnect

fake_client2 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": mock.Mock(),
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client2), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._reset_keepalive"):
    df.start_live_feed(["RELIANCE"], _is_reconnect=True)

assert df._reconnect_attempts == 3, f"TEST 2 FAIL: counter should still be 3, got {df._reconnect_attempts}"
cancel_pending_timer()
print("TEST 2 PASS: start_live_feed(_is_reconnect=True) preserves counter")


# ── TEST 3: _do_reconnect() increments counter on failure, does not reset ──────
reset_module()
df._reconnect_attempts = 0

def fail_subscribe(**kw):
    raise ConnectionError("Broker down")

fake_client3 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": fail_subscribe,
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client3), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._reconnect_timer", None), \
     mock.patch("core.data_fetcher._is_market_open", return_value=True):
    # Directly invoke _do_reconnect logic but with _schedule_reconnect patched to
    # avoid spawning real timers
    with mock.patch("core.data_fetcher._schedule_reconnect") as mock_sched:
        df._do_reconnect()

assert df._reconnect_attempts == 1, f"TEST 3 FAIL: expected 1 after one failure, got {df._reconnect_attempts}"
cancel_pending_timer()
print("TEST 3 PASS: _do_reconnect() increments counter on failure, does not reset")


# ── TEST 4: Counter reaches max_reconnect → alert fires ────────────────────────
reset_module()
df._reconnect_attempts = df._max_reconnect - 1   # one below max

def fail_subscribe2(**kw):
    raise ConnectionError("Still down")

fake_client4 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": fail_subscribe2,
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client4), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._is_market_open", return_value=True):
    df._do_reconnect()

assert df._reconnect_attempts == df._max_reconnect, (
    f"TEST 4 FAIL: expected {df._max_reconnect}, got {df._reconnect_attempts}"
)
assert len(alerts_sent) == 1, f"TEST 4 FAIL: expected 1 alert, got {len(alerts_sent)}"
assert "DEAD" in alerts_sent[0] or "reconnect" in alerts_sent[0].lower(), (
    "TEST 4 FAIL: alert content unexpected: " + str(alerts_sent[0])
)
cancel_pending_timer()
print("TEST 4 PASS: Feed-death alert fires when counter reaches max_reconnect")


# ── TEST 5: Successful reconnect resets counter ────────────────────────────────
reset_module()
df._reconnect_attempts = 4   # mid-sequence

fake_client5 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": mock.Mock(),
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client5), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._reset_keepalive"):
    df._do_reconnect()

assert df._reconnect_attempts == 0, (
    f"TEST 5 FAIL: counter should reset to 0 on success, got {df._reconnect_attempts}"
)
cancel_pending_timer()
print("TEST 5 PASS: Successful reconnect resets counter to 0")


# ── TEST 6: No double-increment — single failure = counter + 1 ────────────────
reset_module()
df._reconnect_attempts = 2

def fail_generic(**kw):
    raise RuntimeError("Protocol error")

fake_client6 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": fail_generic,
})()

with mock.patch("core.kotak_client.get_client", return_value=fake_client6), \
     mock.patch("core.data_fetcher.resolve_instrument_tokens",
                return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
     mock.patch("core.data_fetcher._publish_feed_stats"), \
     mock.patch("core.data_fetcher._is_market_open", return_value=True), \
     mock.patch("core.data_fetcher._schedule_reconnect"):
    df._do_reconnect()

assert df._reconnect_attempts == 3, (
    f"TEST 6 FAIL: expected exactly 3 (2+1), got {df._reconnect_attempts}"
)
cancel_pending_timer()
print("TEST 6 PASS: Single failure increments counter by exactly 1 (no double-increment)")


# ── TEST 7: startup subscribe failure schedules reconnect (doesn't crash) ──────
reset_module()
df._reconnect_attempts = 0

def fail_startup(**kw):
    raise ConnectionError("Startup subscribe error")

fake_client7 = type("C", (), {
    "on_message": None, "on_open": None, "on_close": None, "on_error": None,
    "subscribe": fail_startup,
})()

crashed = [False]
try:
    with mock.patch("core.kotak_client.get_client", return_value=fake_client7), \
         mock.patch("core.data_fetcher.resolve_instrument_tokens",
                    return_value=[{"instrument_token": "123", "exchange_segment": "nse_cm"}]), \
         mock.patch("core.data_fetcher._publish_feed_stats"), \
         mock.patch("core.data_fetcher._is_market_open", return_value=True), \
         mock.patch("core.data_fetcher._schedule_reconnect") as mock_sched7:
        df.start_live_feed(["RELIANCE"], _is_reconnect=False)
        assert mock_sched7.called, "TEST 7 FAIL: _schedule_reconnect should be called"
except Exception as e:
    crashed[0] = True
    print(f"TEST 7 FAIL: startup raised exception unexpectedly: {e}")

assert not crashed[0], "TEST 7 FAIL: start_live_feed should not raise on initial startup failure"
# counter should NOT be reset because the subscribe failed — but the initial startup path
# resets counter BEFORE the subscribe attempt, so it should be 0
assert df._reconnect_attempts == 0, (
    f"TEST 7 FAIL: initial startup counter should be 0, got {df._reconnect_attempts}"
)
cancel_pending_timer()
print("TEST 7 PASS: Initial startup subscribe failure calls _schedule_reconnect, does not crash")


print()
print("ALL 7 TESTS PASSED")
