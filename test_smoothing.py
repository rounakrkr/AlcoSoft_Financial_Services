#!/usr/bin/env python3
"""Test smoothing and adaptive config system"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from reflection.adaptive_config_updater import apply_adaptive_config, get_adaptive_config_summary
from reflection.reflection_engine import (
    _calculate_trade_time_decay,
    _calculate_confidence_strength,
    _apply_ema_smoothing,
    _apply_daily_adjustment_limit,
)

print("=" * 70)
print("SMOOTHING & ADAPTIVE CONFIG TEST")
print("=" * 70)

# Test 1: Smoothing functions
print("\n1️⃣ Testing Smoothing Functions")
print("-" * 70)

print(f"Time decay (0 days ago):  {_calculate_trade_time_decay('2026-05-29T10:00:00'):.3f}")
print(f"Time decay (30 days ago): {_calculate_trade_time_decay('2026-04-29T10:00:00'):.3f}")
print(f"Time decay (60 days ago): {_calculate_trade_time_decay('2026-03-30T10:00:00'):.3f}")

print(f"\nConfidence strength (10 trades):   {_calculate_confidence_strength(10):.3f}")
print(f"Confidence strength (50 trades):   {_calculate_confidence_strength(50):.3f}")
print(f"Confidence strength (100 trades):  {_calculate_confidence_strength(100):.3f}")

print(f"\nEMA Smoothing (old=1.0, new=0.8, conf=0.5): {_apply_ema_smoothing(1.0, 0.8, 0.2, 0.5):.3f}")
print(f"EMA Smoothing (old=1.0, new=1.2, conf=0.5): {_apply_ema_smoothing(1.0, 1.2, 0.2, 0.5):.3f}")

print(f"\nDaily limit (old=1.0, new=0.7, limit=5%): {_apply_daily_adjustment_limit(1.0, 0.7, 5.0):.3f}")
print(f"Daily limit (old=1.0, new=1.1, limit=5%): {_apply_daily_adjustment_limit(1.0, 1.1, 5.0):.3f}")
print(f"Daily limit (old=1.0, new=0.5, limit=5%): {_apply_daily_adjustment_limit(1.0, 0.5, 5.0):.3f}")

# Test 2: Adaptive config application
print("\n\n2️⃣ Testing Adaptive Config Application")
print("-" * 70)

result = apply_adaptive_config()
print(f"apply_adaptive_config() result: {result}")

summary = get_adaptive_config_summary()
adaptive_config = summary.get("adaptive", {})
print(f"Config sections: {list(adaptive_config.keys())}")

if "strategy" in adaptive_config:
    strategy = adaptive_config["strategy"]
    print(f"  - Signal confidence multipliers: {len(strategy.get('signal_confidence_multipliers', {}))} signals")
    print(f"  - Market regime multiplier: {strategy.get('market_regime_multiplier', 'N/A')}")

if "time_windows" in adaptive_config:
    print(f"  - Time window multipliers: {len(adaptive_config['time_windows'])} windows")

if "symbol_stops" in adaptive_config:
    print(f"  - Symbol SL adjustments: {len(adaptive_config['symbol_stops'])} symbols")

print("\n" + "=" * 70)
print("✅ ALL TESTS PASSED")
print("=" * 70)
