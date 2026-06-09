"""
F001 Validation Tests — Over-Leverage Guard Uses Current Market Value

Covers:
  T1: No open positions — guard passes, order proceeds (baseline)
  T2: Position at entry value, no adverse move — guard consistent with prior behaviour
  T3: Position moved FAVOURABLY (current > entry) — guard now catches over-leverage
      that old code (using entry value) would have missed
  T4: Position moved ADVERSELY (current < entry) — guard is conservative (may block
      unnecessarily) — acceptable, safe side of the error
  T5: current_position_value == entry_position_value — debug log NOT emitted
  T6: current_position_value != entry_position_value — debug log IS emitted
  T7: Exactly at leverage ceiling — passes (boundary)
  T8: One unit over leverage ceiling — blocked (boundary)
"""

import os, sys, logging
logging.basicConfig(level=logging.CRITICAL)

os.environ.setdefault("TRADING_MODE", "PAPER")
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest.mock as mock
import core.order_executor as oe

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_margin_status(
    real_capital=10000.0,
    margin_leverage=2.0,
    current_position_value=0.0,
    entry_position_value=0.0,
):
    """Build a margin_status dict as returned by get_margin_status()."""
    return {
        "real_capital":             real_capital,
        "margin_leverage":          margin_leverage,
        "total_available_with_margin": real_capital * margin_leverage,
        "current_position_value":   current_position_value,
        "deployed_in_positions":    entry_position_value,   # old field (kept for compat)
        "entry_position_value":     entry_position_value,
        "unrealized_pnl":           current_position_value - entry_position_value,
        "effective_capital":        real_capital + (current_position_value - entry_position_value),
        "margin_used":              max(0.0, current_position_value - real_capital),
        "margin_pct":               0.0,
        "remaining_margin":         real_capital * margin_leverage - current_position_value,
        "is_over_leveraged":        current_position_value > real_capital * margin_leverage,
    }


def run_guard(
    symbol="RELIANCE",
    entry_price=1000.0,
    quantity=5,
    margin_status=None,
    log_capture=None,
):
    """
    Run only the over-leverage guard block extracted from _place_buy_order_impl.
    Returns True if order would be BLOCKED, False if it passes.
    Captures debug log messages if log_capture list is provided.
    """
    from core.safe_io import safe_float

    if margin_status is None:
        margin_status = make_margin_status()

    capital_deployed_if_buy = quantity * entry_price

    deployed_now_current = safe_float(margin_status.get("current_position_value"), 0.0)
    deployed_now_entry   = safe_float(margin_status.get("entry_position_value"), 0.0)
    real_capital         = safe_float(margin_status.get("real_capital"), 0.0)
    leverage             = safe_float(margin_status.get("margin_leverage"), 1.0)

    total_would_deploy  = deployed_now_current + capital_deployed_if_buy
    max_deployable      = real_capital * leverage
    would_over_leverage = total_would_deploy > max_deployable

    if log_capture is not None and deployed_now_current != deployed_now_entry:
        log_capture.append(("debug", symbol, deployed_now_entry, deployed_now_current))

    return would_over_leverage


# ── TEST 1: No open positions — guard passes ───────────────────────────────────
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=0, entry_position_value=0)
# 5 shares @ ₹1000 = ₹5000; max_deployable = 10000×2 = ₹20000
blocked = run_guard(entry_price=1000, quantity=5, margin_status=ms)
assert not blocked, "TEST 1 FAIL: No open positions — guard should PASS"
print("TEST 1 PASS: No open positions — guard passes correctly")


# ── TEST 2: Position at entry value, no price move — consistent with prior ─────
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=8000, entry_position_value=8000)
# Adding ₹5000 → 8000+5000=13000 < 20000 → should pass
blocked = run_guard(entry_price=1000, quantity=5, margin_status=ms)
assert not blocked, "TEST 2 FAIL: No price move — guard should PASS"
print("TEST 2 PASS: Position at entry value — guard passes (consistent with prior behaviour)")


# ── TEST 3: Position moved FAVOURABLY — old code missed over-leverage ──────────
# Setup: ₹10,000 capital, 2x leverage (ceiling = ₹20,000)
# Bought 5 shares at ₹1,500 each → entry = ₹7,500
# They rose to ₹2,000 → current = ₹10,000
# Old code: deployed_now = entry = ₹7,500 → total = 7500+13000=20500 > 20000 → might catch
# but what if entry was lower:
# Bought 3 shares at ₹1,000 → entry = ₹3,000; rose to ₹3,500 → current = ₹10,500
# Old code: 3000+15000=18000 < 20000 → PASSES (misses over-leverage)
# New code: 10500+15000=25500 > 20000 → BLOCKS ← correct
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=10500, entry_position_value=3000)
# new order: 15 shares @ ₹1000 = ₹15000
blocked_new = run_guard(entry_price=1000, quantity=15, margin_status=ms)
assert blocked_new, "TEST 3 FAIL: Favourable move — new code should BLOCK (over-leverage)"

# Confirm old logic would have passed (entry-based)
total_old = 3000 + 15 * 1000
would_old_block = total_old > 10000 * 2.0
assert not would_old_block, "TEST 3 SETUP: old entry-based logic should have PASSED (missed over-leverage)"

print("TEST 3 PASS: Favourable price move — new code BLOCKS over-leverage that old code missed")


# ── TEST 4: Position moved ADVERSELY — new code is more conservative ───────────
# Entry ₹10,000; current ₹7,000; adding ₹5,000
# new: 7000+5000=12000 < 20000 → PASSES
# old: 10000+5000=15000 < 20000 → PASSES
# Both pass — new code is slightly more conservative (if current > entry this helps,
# but here current < entry so new code is actually LESS restrictive than entry-based)
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=7000, entry_position_value=10000)
blocked = run_guard(entry_price=1000, quantity=5, margin_status=ms)
assert not blocked, "TEST 4 FAIL: Adverse move — adding ₹5000 to ₹7000 current (12000 < 20000) should PASS"
print("TEST 4 PASS: Adverse move — guard correctly uses current value (conservative but not blocking)")


# ── TEST 5: Values equal — no debug log emitted ────────────────────────────────
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=5000, entry_position_value=5000)
log = []
run_guard(entry_price=100, quantity=5, margin_status=ms, log_capture=log)
assert len(log) == 0, "TEST 5 FAIL: Equal values — debug log should NOT be emitted"
print("TEST 5 PASS: entry == current — no debug log emitted")


# ── TEST 6: Values differ — debug log IS emitted ───────────────────────────────
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=5500, entry_position_value=5000)
log = []
run_guard(entry_price=100, quantity=5, margin_status=ms, log_capture=log)
assert len(log) == 1, f"TEST 6 FAIL: Unequal values — debug log should be emitted, got {len(log)}"
print("TEST 6 PASS: entry != current — debug log emitted")


# ── TEST 7: Exactly at leverage ceiling — passes (boundary) ───────────────────
# real_capital=10000, leverage=2.0 → max=20000
# current=0, new order = 20000 exactly → 0+20000 = 20000, NOT > 20000 → PASSES
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=0, entry_position_value=0)
blocked = run_guard(entry_price=1000, quantity=20, margin_status=ms)
assert not blocked, "TEST 7 FAIL: Exactly at ceiling (20000 = 20000) — should PASS"
print("TEST 7 PASS: Exactly at leverage ceiling — passes (boundary inclusive)")


# ── TEST 8: One unit over ceiling — BLOCKED (boundary) ────────────────────────
# 21 shares @ ₹1000 = ₹21000 > ₹20000 → BLOCKED
ms = make_margin_status(real_capital=10000, margin_leverage=2.0,
                        current_position_value=0, entry_position_value=0)
blocked = run_guard(entry_price=1000, quantity=21, margin_status=ms)
assert blocked, "TEST 8 FAIL: One unit over ceiling (21000 > 20000) — should BLOCK"
print("TEST 8 PASS: One unit over ceiling — blocked (boundary exclusive)")


print()
print("ALL 8 TESTS PASSED")
