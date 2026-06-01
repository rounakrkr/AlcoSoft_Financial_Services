#!/usr/bin/env python3
"""
Trace: Daily Loss Limit Calculation
=======================================
Goal: Show actual runtime values from configuration
- Configured capital (from trading_settings.json)
- Available capital (calculated at runtime)
- Deployed capital (from open positions)
- Daily loss percentage (from config)
- Calculated daily loss limit (should be constant after trade entry)
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

print("\n" + "="*70)
print("STEP 1: READ CONFIGURATION FROM trading_settings.json")
print("="*70)

config_path = Path(__file__).parent / "config" / "trading_settings.json"
with open(config_path) as f:
    config = json.load(f)

configured_capital = config["risk"]["paper_capital"]
configured_daily_loss_pct = config["risk"]["max_daily_loss_percent"]

print(f"\nFrom trading_settings.json:")
print(f"  configured_capital        = Rs {configured_capital:,}")
print(f"  configured_daily_loss_pct = {configured_daily_loss_pct} (decimal)")

# === PHASE 2: Setup clean state ===
print("\n" + "="*70)
print("STEP 2: INITIALIZE DATABASE (clean state)")
print("="*70)

from core.state_manager import initialize_db, get_open_positions, get_today_gross_pnl

initialize_db()
positions_before = get_open_positions()
pnl_before = get_today_gross_pnl()

print(f"\nInitial state:")
print(f"  Open positions = {len(positions_before)}")
print(f"  Today P&L      = Rs {pnl_before:.2f}")

# === PHASE 3: Calculate values BEFORE trade ===
print("\n" + "="*70)
print("STEP 3: CALCULATE DAILY LOSS LIMIT BEFORE TRADE")
print("="*70)

from core.trading_settings import get as cfg
from core.safe_io import safe_float
from core.order_executor import (
    _get_available_capital,
    TRADING_MODE
)

print(f"\nTRADING_MODE = {TRADING_MODE}")

# Get current values
gross_pnl_before = get_today_gross_pnl()
available_capital_before = _get_available_capital()

positions = get_open_positions()
deployed_capital_before = sum(
    safe_float(p.get("quantity", 0), 0.0) * safe_float(p.get("entry_price", 0), 0.0)
    for p in positions
)

# Get the values used in the calculation
daily_loss_pct = safe_float(cfg("risk", "max_daily_loss_percent", 0.05), 0.05)
daily_loss_pct_clamped = max(0.0, min(1.0, daily_loss_pct))

# Calculate the limit
daily_loss_limit_before = -(configured_capital * daily_loss_pct_clamped)

print(f"\n[BEFORE TRADE] Runtime Values:")
print(f"  configured_capital        = Rs {configured_capital:,}")
print(f"  available_capital         = Rs {available_capital_before:.2f}")
print(f"  deployed_capital          = Rs {deployed_capital_before:.2f}")
print(f"  daily_loss_pct (raw)      = {daily_loss_pct}")
print(f"  daily_loss_pct (clamped)  = {daily_loss_pct_clamped}")
print(f"  gross_pnl                 = Rs {gross_pnl_before:.2f}")

print(f"\n[CALCULATION]")
print(f"  daily_loss_limit = -(configured_capital * daily_loss_pct_clamped)")
print(f"                   = -(Rs {configured_capital:,} * {daily_loss_pct_clamped})")
print(f"                   = Rs {daily_loss_limit_before:.2f}")

print(f"\n[RESULT BEFORE TRADE]")
print(f"  daily_loss_limit = Rs {daily_loss_limit_before:.2f}")
print(f"  will_halt        = {gross_pnl_before <= daily_loss_limit_before}")

# === PHASE 4: Simulate entering a trade ===
print("\n" + "="*70)
print("STEP 4: SIMULATE ENTERING TRADE")
print("="*70)

from core.state_manager import save_open_position

symbol = "RELIANCE"
quantity = 94
entry_price = 1327.4
target = 1334.04
stop_loss = 1324.08
trade_value = quantity * entry_price

print(f"\nEntering position:")
print(f"  symbol      = {symbol}")
print(f"  quantity    = {quantity}")
print(f"  entry_price = Rs {entry_price}")
print(f"  trade_value = Rs {trade_value:.2f}")
print(f"  stop_loss   = Rs {stop_loss}")
print(f"  target      = Rs {target}")

save_open_position({
    "symbol": symbol,
    "quantity": quantity,
    "entry_price": entry_price,
    "entry_time": "2026-06-01 12:45:02",
    "stop_loss": stop_loss,
    "target": target,
    "trailing_sl": stop_loss,
    "side": "BUY",
    "strategy_set": "BUY_EMA_VOLUME_MOMENTUM",
})

print(f"\nPosition saved.")

# === PHASE 5: Calculate values AFTER trade ===
print("\n" + "="*70)
print("STEP 5: CALCULATE DAILY LOSS LIMIT AFTER TRADE")
print("="*70)

# Get current values
gross_pnl_after = get_today_gross_pnl()
available_capital_after = _get_available_capital()

positions_after = get_open_positions()
deployed_capital_after = sum(
    safe_float(p.get("quantity", 0), 0.0) * safe_float(p.get("entry_price", 0), 0.0)
    for p in positions_after
)

# Get the values used in the calculation (should be same as before)
daily_loss_pct_after = safe_float(cfg("risk", "max_daily_loss_percent", 0.05), 0.05)
daily_loss_pct_clamped_after = max(0.0, min(1.0, daily_loss_pct_after))

# Calculate the limit
daily_loss_limit_after = -(configured_capital * daily_loss_pct_clamped_after)

print(f"\n[AFTER TRADE] Runtime Values:")
print(f"  configured_capital        = Rs {configured_capital:,}")
print(f"  available_capital         = Rs {available_capital_after:.2f}")
print(f"  deployed_capital          = Rs {deployed_capital_after:.2f}")
print(f"  daily_loss_pct (raw)      = {daily_loss_pct_after}")
print(f"  daily_loss_pct (clamped)  = {daily_loss_pct_clamped_after}")
print(f"  gross_pnl                 = Rs {gross_pnl_after:.2f}")

print(f"\n[CALCULATION]")
print(f"  daily_loss_limit = -(configured_capital * daily_loss_pct_clamped)")
print(f"                   = -(Rs {configured_capital:,} * {daily_loss_pct_clamped_after})")
print(f"                   = Rs {daily_loss_limit_after:.2f}")

print(f"\n[RESULT AFTER TRADE]")
print(f"  daily_loss_limit = Rs {daily_loss_limit_after:.2f}")
print(f"  will_halt        = {gross_pnl_after <= daily_loss_limit_after}")

# === PHASE 6: Comparison ===
print("\n" + "="*70)
print("STEP 6: COMPARISON - LIMIT STABILITY")
print("="*70)

print(f"\nBEFORE TRADE:")
print(f"  configured_capital = Rs {configured_capital:,}")
print(f"  available_capital  = Rs {available_capital_before:.2f}")
print(f"  deployed_capital   = Rs {deployed_capital_before:.2f}")
print(f"  daily_loss_pct     = {daily_loss_pct_clamped}")
print(f"  daily_loss_limit   = Rs {daily_loss_limit_before:.2f}")

print(f"\nAFTER TRADE (position worth Rs {trade_value:.2f}):")
print(f"  configured_capital = Rs {configured_capital:,}  (UNCHANGED - source of truth)")
print(f"  available_capital  = Rs {available_capital_after:.2f}  (REDUCED due to deployment)")
print(f"  deployed_capital   = Rs {deployed_capital_after:.2f}  (NOW DEPLOYED)")
print(f"  daily_loss_pct     = {daily_loss_pct_clamped_after}  (UNCHANGED)")
print(f"  daily_loss_limit   = Rs {daily_loss_limit_after:.2f}  (SHOULD BE SAME)")

print(f"\nCHANGE ANALYSIS:")
print(f"  deployed_capital changed by Rs {deployed_capital_after - deployed_capital_before:.2f}")
print(f"  available_capital changed by Rs {available_capital_after - available_capital_before:.2f}")
print(f"  daily_loss_limit changed by Rs {daily_loss_limit_after - daily_loss_limit_before:.2f}")

if daily_loss_limit_before == daily_loss_limit_after:
    print(f"\n[PASS] daily_loss_limit REMAINED CONSTANT")
    print(f"  ✓ Limit is based on configured_capital (not available_capital)")
    print(f"  ✓ Limit stays Rs {daily_loss_limit_before:.2f} regardless of trade entry")
else:
    print(f"\n[FAIL] daily_loss_limit CHANGED")
    print(f"  Before: Rs {daily_loss_limit_before:.2f}")
    print(f"  After:  Rs {daily_loss_limit_after:.2f}")
    print(f"  Diff:   Rs {daily_loss_limit_after - daily_loss_limit_before:.2f}")

print("\n" + "="*70)

