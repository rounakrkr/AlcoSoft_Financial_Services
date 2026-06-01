#!/usr/bin/env python3
# ============================================================
#   Test: Capital Allocation Redesign (2026-06-01)
#   Tests all 4 scenarios with new multi-constraint model
# ============================================================

import sys
import logging
from decimal import Decimal

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("TEST_ALLOCATION")

# Test scenarios
SCENARIOS = {
    "A": {
        "name": "Margin OFF, 4 Positions",
        "config": {
            "capital": 100000,
            "allow_margin": False,
            "margin_leverage": 2.0,
            "position_size_margin": 1.0,
            "max_open_positions": 4,
            "risk_pct": 0.02,
        },
        "positions": 4,
        "entry_price": 1000,
        "stop_loss": 997.50,
        "expected": {
            "per_position_budget": 25000,
            "qty_per_position": 25,
            "total_deployed": 100000,
        },
    },
    "B": {
        "name": "Margin ON (5x), 4 Positions",
        "config": {
            "capital": 100000,
            "allow_margin": True,
            "margin_leverage": 5.0,
            "position_size_margin": 1.0,
            "max_open_positions": 4,
            "risk_pct": 0.02,
        },
        "positions": 4,
        "entry_price": 1000,
        "stop_loss": 997.50,
        "expected": {
            "per_position_budget": 125000,
            "qty_per_position": 125,  # Now allocation-limited (capital constraint removed)
            "total_deployed": 500000,
            "limiting_constraint": "allocation",  # All 4 positions limited by allocation budget
        },
    },
    "C": {
        "name": "Margin ON (5x), Conservative (50%)",
        "config": {
            "capital": 100000,
            "allow_margin": True,
            "margin_leverage": 5.0,
            "position_size_margin": 0.50,
            "max_open_positions": 4,
            "risk_pct": 0.02,
        },
        "positions": 4,
        "entry_price": 1000,
        "stop_loss": 997.50,
        "expected": {
            "per_position_budget": 62500,
            "qty_per_position": 62,
            "total_deployed": 248000,
        },
    },
    "D": {
        "name": "Margin ON (5x), 2 Positions (vs 4)",
        "config": {
            "capital": 100000,
            "allow_margin": True,
            "margin_leverage": 5.0,
            "position_size_margin": 1.0,
            "max_open_positions": 2,  # Different from B
            "risk_pct": 0.02,
        },
        "positions": 2,
        "entry_price": 1000,
        "stop_loss": 997.50,
        "expected": {
            "per_position_budget": 250000,  # ₹500k / 2
            "qty_per_position": 250,  # Now allocation-limited (capital constraint removed)
            "total_deployed": 500000,  # Can open 2 positions @ ₹250k each
            "limiting_constraint": "allocation",
        },
    },
}


def simulate_allocation(scenario: dict) -> dict:
    """
    Simulate allocation calculations for a scenario.
    Returns dict with results.
    """
    cfg = scenario["config"]
    
    # Calculate buying power
    real_capital = cfg["capital"]
    margin_leverage = cfg["margin_leverage"] if cfg["allow_margin"] else 1.0
    total_buying_power = real_capital * margin_leverage
    
    # Calculate per-position budget
    position_size_margin = cfg["position_size_margin"]
    max_positions = cfg["max_open_positions"]
    
    allocated_capital = total_buying_power * position_size_margin
    per_position_budget = allocated_capital / max_positions
    
    # Calculate constraints for first position (no deployment yet)
    price = scenario["entry_price"]
    stop_loss = scenario["stop_loss"]
    risk_pct = cfg["risk_pct"]
    
    # Risk constraint
    max_loss = real_capital * risk_pct
    stop_dist = abs(price - stop_loss)
    risk_qty = max_loss / stop_dist if stop_dist > 0 else 0
    
    # Allocation constraint
    allocation_qty = per_position_budget / price
    
    # Leverage constraint
    leverage_qty = total_buying_power / price
    
    # Final quantity (MIN of 3 constraints, capital removed)
    qty = int(min(risk_qty, allocation_qty, leverage_qty))
    
    # Identify limiting constraint
    constraints = {
        'risk': int(risk_qty),
        'allocation': int(allocation_qty),
        'leverage': int(leverage_qty),
    }
    
    limiting = min(constraints.items(), key=lambda x: x[1])[0]
    
    return {
        'total_buying_power': total_buying_power,
        'per_position_budget': per_position_budget,
        'qty': qty,
        'constraints': constraints,
        'limiting_constraint': limiting,
        'total_deployed_4_positions': qty * price * 4,
    }


def run_tests():
    """Run all scenario tests."""
    logger.info("=" * 80)
    logger.info("CAPITAL ALLOCATION REDESIGN - SCENARIO TESTS")
    logger.info("=" * 80)
    
    all_passed = True
    
    for scenario_id, scenario in SCENARIOS.items():
        logger.info("")
        logger.info(f"SCENARIO {scenario_id}: {scenario['name']}")
        logger.info("-" * 80)
        
        result = simulate_allocation(scenario)
        expected = scenario["expected"]
        
        # Display calculations
        logger.info(f"Configuration:")
        logger.info(f"  Capital: ₹{scenario['config']['capital']:,}")
        logger.info(f"  Margin: {'ON' if scenario['config']['allow_margin'] else 'OFF'} "
                   f"({scenario['config']['margin_leverage']}x leverage)")
        logger.info(f"  Position Size Margin: {scenario['config']['position_size_margin']*100:.0f}%")
        logger.info(f"  Max Open Positions: {scenario['config']['max_open_positions']}")
        logger.info(f"  Risk Per Trade: {scenario['config']['risk_pct']*100:.1f}%")
        
        logger.info(f"Calculations:")
        logger.info(f"  Total Buying Power: ₹{result['total_buying_power']:,.0f}")
        logger.info(f"  Per-Position Budget: ₹{result['per_position_budget']:,.0f}")
        logger.info(f"  Entry Price: ₹{scenario['entry_price']}")
        logger.info(f"  Stop Loss: ₹{scenario['stop_loss']}")
        
        logger.info(f"Constraints (per position):")
        logger.info(f"  Risk-based qty: {result['constraints']['risk']} shares")
        logger.info(f"  Allocation-based qty: {result['constraints']['allocation']} shares")
        logger.info(f"  Leverage-based qty: {result['constraints']['leverage']} shares")
        logger.info(f"  → Limiting constraint: {result['limiting_constraint'].upper()}")
        
        logger.info(f"Results:")
        logger.info(f"  Final Qty (per position): {result['qty']} shares")
        logger.info(f"  Capital per position: ₹{result['qty'] * scenario['entry_price']:,.0f}")
        logger.info(f"  Total deployed ({scenario['positions']} positions): ₹{result['qty'] * scenario['entry_price'] * scenario['positions']:,.0f}")
        
        # Validation
        logger.info(f"Validation:")
        per_pos_match = abs(result['per_position_budget'] - expected['per_position_budget']) < 1
        qty_match = abs(result['qty'] - expected['qty_per_position']) < 1
        constraint_match = result['limiting_constraint'] == expected.get('limiting_constraint', result['limiting_constraint'])
        
        if per_pos_match:
            logger.info(f"  ✅ Per-position budget: ₹{result['per_position_budget']:,.0f} "
                       f"(expected ₹{expected['per_position_budget']:,.0f})")
        else:
            logger.error(f"  ❌ Per-position budget: ₹{result['per_position_budget']:,.0f} "
                        f"(expected ₹{expected['per_position_budget']:,.0f})")
            all_passed = False
        
        if qty_match:
            logger.info(f"  ✅ Qty per position: {result['qty']} "
                       f"(expected {expected['qty_per_position']})")
        else:
            logger.error(f"  ❌ Qty per position: {result['qty']} "
                        f"(expected {expected['qty_per_position']})")
            all_passed = False
        
        if constraint_match:
            logger.info(f"  ✅ Limiting constraint: {result['limiting_constraint'].upper()} "
                       f"(expected {expected.get('limiting_constraint', '?').upper()})")
        else:
            logger.warning(f"  ⚠️  Limiting constraint: {result['limiting_constraint'].upper()} "
                          f"(expected {expected.get('limiting_constraint', '?').upper()})")
        
        logger.info("")
    
    # Final summary
    logger.info("=" * 80)
    if all_passed:
        logger.info("✅ ALL SCENARIOS PASSED")
    else:
        logger.error("❌ SOME SCENARIOS FAILED")
    logger.info("=" * 80)
    
    return all_passed


if __name__ == "__main__":
    passed = run_tests()
    sys.exit(0 if passed else 1)
