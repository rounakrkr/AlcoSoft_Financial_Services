"""
F002 v3 Validation Tests — State Representation Fix
Covers: kotak_sl_order_id as sole recovery signal, notes-field independence,
Scenario C immunity, and all prior throttling behaviours preserved.
"""
import os
import sys
import datetime
import logging

logging.basicConfig(level=logging.WARNING)

os.environ["TRADING_MODE"]  = "LIVE"
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.order_executor as oe
import core.state_manager  as sm
from core.circuit_breaker import get_breaker, CircuitState

call_log = []

def make_pos(symbol, kotak_sl_order_id="", notes="", stop_loss=2400.0):
    return {
        "symbol": symbol,
        "kotak_sl_order_id": kotak_sl_order_id,
        "notes": notes,
        "stop_loss": stop_loss,
        "trailing_sl": 0.0,
        "quantity": 1,
        "trading_symbol": symbol + "-EQ",
        "product": "MIS",
    }

def reset(sl_fn=None, positions=None):
    oe._sl_recovery_state.clear()
    call_log.clear()
    oe.get_open_positions   = lambda: (positions or [])
    oe._send_kotak_sl_order = sl_fn or (lambda **kw: (call_log.append(1) or None))
    oe.TRADING_MODE = "LIVE"
    b = get_breaker("order")
    b.state = CircuitState.CLOSED
    b.failure_count = 0
    b.last_failure_time = None


# ── TEST 1: Position with empty kotak_sl_order_id triggers recovery ────────
reset(positions=[make_pos("RELIANCE", kotak_sl_order_id="")])
oe.attempt_broker_sl_recovery()
assert len(call_log) == 1, "TEST 1 FAIL: empty kotak_sl_order_id should trigger recovery"
print("TEST 1 PASS: Empty kotak_sl_order_id triggers recovery")

# ── TEST 2: Position with populated kotak_sl_order_id is silently skipped ──
reset(positions=[make_pos("RELIANCE", kotak_sl_order_id="SL-99999")])
oe.attempt_broker_sl_recovery()
assert call_log == [], "TEST 2 FAIL: populated kotak_sl_order_id should be skipped"
print("TEST 2 PASS: Populated kotak_sl_order_id is silently skipped")

# ── TEST 3: notes field content is IRRELEVANT to recovery eligibility ───────
# An unprotected position (empty sl_order_id) whose notes were clobbered by
# reconciliation (Scenario C) must STILL be recovered.
reset(positions=[make_pos("RELIANCE",
    kotak_sl_order_id="",
    notes="Broker reconciliation repaired local quantity/entry")])
oe.attempt_broker_sl_recovery()
assert len(call_log) == 1, (
    "TEST 3 FAIL: clobbered notes must not prevent recovery (Scenario C)")
print("TEST 3 PASS: Scenario C immunity — notes clobber does not disable recovery")

# ── TEST 4: Old SL_BROKER_UNPROTECTED flag in notes is ignored (no special ─
#            handling, no parsing — it is inert if kotak_sl_order_id is set)
reset(positions=[make_pos("RELIANCE",
    kotak_sl_order_id="SL-EXISTING",
    notes="SL_BROKER_UNPROTECTED")])  # old-style flag + real SL id
oe.attempt_broker_sl_recovery()
assert call_log == [], "TEST 4 FAIL: populated sl_order_id should be skipped even if old flag in notes"
print("TEST 4 PASS: Old SL_BROKER_UNPROTECTED note ignored when kotak_sl_order_id is set")

# ── TEST 5: PAPER mode is no-op regardless of kotak_sl_order_id ────────────
oe.TRADING_MODE = "PAPER"
oe.get_open_positions = lambda: [make_pos("RELIANCE", kotak_sl_order_id="")]
oe.attempt_broker_sl_recovery()
assert call_log == [], "TEST 5 FAIL: PAPER mode should never call broker"
print("TEST 5 PASS: PAPER mode no-op")
oe.TRADING_MODE = "LIVE"

# ── TEST 6: Circuit breaker OPEN prevents recovery ─────────────────────────
reset(positions=[make_pos("RELIANCE", kotak_sl_order_id="")])
b = get_breaker("order")
b.state = CircuitState.OPEN
b.last_failure_time = datetime.datetime.now()
oe.attempt_broker_sl_recovery()
assert call_log == [], "TEST 6 FAIL: circuit OPEN must prevent all calls"
b.state = CircuitState.CLOSED; b.failure_count = 0
print("TEST 6 PASS: Circuit OPEN prevents recovery")

# ── TEST 7: Cooldown backoff still works with the new eligibility check ────
reset(positions=[make_pos("RELIANCE", kotak_sl_order_id="")])
oe.attempt_broker_sl_recovery()   # attempt 1 — fires
assert len(call_log) == 1, "TEST 7 FAIL: first call should fire"
oe.attempt_broker_sl_recovery()   # attempt 2 — blocked by cooldown
assert len(call_log) == 1, "TEST 7 FAIL: second call should be blocked by cooldown"
s = oe._sl_recovery_state["RELIANCE"]
assert s["next_delay_sec"] == oe._SL_RECOVERY_INITIAL_DELAY_SEC * oe._SL_RECOVERY_BACKOFF_FACTOR
print("TEST 7 PASS: Exponential backoff still operates correctly")

# ── TEST 8: Success sets kotak_sl_order_id — position exits recovery ────────
db_calls = []
sm.update_sl_order_id = lambda sym, sid: db_calls.append(("sl", sym, sid))

def sl_ok(**kw): call_log.append(1); return "RECOVERED-SL-777"

reset(sl_fn=sl_ok, positions=[make_pos("RELIANCE", kotak_sl_order_id="")])
oe.attempt_broker_sl_recovery()
assert any(c[0] == "sl" for c in db_calls), "TEST 8 FAIL: update_sl_order_id not called on success"
assert "RELIANCE" not in oe._sl_recovery_state, "TEST 8 FAIL: backoff state not cleared on success"

# Simulate what happens next loop: position now has kotak_sl_order_id set
# (As if the DB query returned the updated record)
reset(sl_fn=sl_ok, positions=[make_pos("RELIANCE", kotak_sl_order_id="RECOVERED-SL-777")])
pre = len(call_log)
oe.attempt_broker_sl_recovery()
assert len(call_log) == pre, "TEST 8 FAIL: position with sl_order_id set should not trigger another attempt"
print("TEST 8 PASS: Success sets sl_order_id — position silently exits recovery on next loop")

# ── TEST 9: No update_position_notes call on success (flag no longer used) ─
notes_calls = []
sm.update_position_notes = lambda sym, n: notes_calls.append((sym, n))
reset(sl_fn=sl_ok, positions=[make_pos("RELIANCE", kotak_sl_order_id="")])
oe.attempt_broker_sl_recovery()
assert notes_calls == [], (
    "TEST 9 FAIL: update_position_notes should NOT be called — flag no longer used")
print("TEST 9 PASS: update_position_notes not called (notes field not used as state carrier)")

# ── TEST 10: Mixed positions — only unprotected ones trigger recovery ────────
reset(positions=[
    make_pos("RELIANCE", kotak_sl_order_id=""),              # unprotected
    make_pos("TCS",      kotak_sl_order_id="SL-TCS-001"),   # protected
    make_pos("INFY",     kotak_sl_order_id=""),              # unprotected
    make_pos("HDFC",     kotak_sl_order_id="SL-HDFC-002"),  # protected
])
oe.attempt_broker_sl_recovery()
assert len(call_log) == 2, (
    "TEST 10 FAIL: expected 2 recovery calls (RELIANCE + INFY), got " + str(len(call_log)))
print("TEST 10 PASS: Mixed positions — only unprotected ones trigger recovery")

print()
print("ALL 10 TESTS PASSED")
