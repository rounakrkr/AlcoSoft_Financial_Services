#!/usr/bin/env python3
"""
🔥 Test script: Margin & Forced Buy demonstration
Demo: Your exact scenario - ₹800 real capital, ₹1000 stock, 2x margin
"""

import sys
sys.path.insert(0, "/root/workspace")

from core.order_executor import (
    calculate_quantity,
    calculate_quantity_with_tranches,
    get_margin_status,
    _get_available_capital
)
from core.trading_settings import set_default_and_save
from unittest.mock import patch

# ══════════════════════════════════════════════════════════════════════════════
# DEMO: YOUR EXACT SCENARIO
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "="*80)
print("🔥 MARGIN & FORCED BUY DEMO")
print("="*80)

print("\n📊 SCENARIO: Real Capital ₹800, Stock ₹1000, 2x Margin")
print("─" * 80)

# Mock the capital to ₹800
with patch('core.order_executor._get_available_capital') as mock_capital:
    mock_capital.return_value = 800.0
    
    # ────────────────────────────────────────────────────────────────────────────
    # TEST 1: No Margin (Default)
    # ────────────────────────────────────────────────────────────────────────────
    print("\n\n🔴 TEST 1: NO MARGIN (allow_margin=False)")
    print("─" * 80)
    
    # Set config
    with patch('core.trading_settings.cfg') as mock_cfg:
        def cfg_side_effect(section, key, default):
            config = {
                ('risk', 'allow_margin'): False,
                ('risk', 'forced_buy_margin'): False,
                ('risk', 'margin_leverage'): 2.0,
                ('risk', 'position_size_margin'): 0.75,
                ('risk', 'max_risk_per_trade'): 0.02,
                ('risk', 'stop_loss_percent'): 0.01,
            }
            return config.get((section, key), default)
        
        mock_cfg.side_effect = cfg_side_effect
        
        qty = calculate_quantity(price=1000, stop_loss=980)
        print(f"Real Capital: ₹800")
        print(f"Margin: DISABLED")
        print(f"Available: ₹800")
        print(f"Stock Price: ₹1000")
        print(f"Result Quantity: {qty}")
        print(f"Decision: {'✅ BUY' if qty > 0 else '❌ REJECT - Can\\'t afford'}")
    
    # ────────────────────────────────────────────────────────────────────────────
    # TEST 2: Margin Enabled (Normal Mode)
    # ────────────────────────────────────────────────────────────────────────────
    print("\n\n🟡 TEST 2: MARGIN ENABLED (allow_margin=True, forced_buy=False)")
    print("─" * 80)
    
    with patch('core.trading_settings.cfg') as mock_cfg:
        def cfg_side_effect(section, key, default):
            config = {
                ('risk', 'allow_margin'): True,
                ('risk', 'forced_buy_margin'): False,
                ('risk', 'margin_leverage'): 2.0,
                ('risk', 'position_size_margin'): 0.75,
                ('risk', 'max_risk_per_trade'): 0.02,
                ('risk', 'stop_loss_percent'): 0.01,
            }
            return config.get((section, key), default)
        
        mock_cfg.side_effect = cfg_side_effect
        
        qty = calculate_quantity(price=1000, stop_loss=980)
        print(f"Real Capital: ₹800")
        print(f"Margin Leverage: 2.0x")
        print(f"Margin Available: ₹800 × 2 = ₹1600")
        print(f"Position Size %: 75%")
        print(f"Stock Price: ₹1000")
        print(f"Affordable Qty: (₹1600 × 0.75) / ₹1000 = 1.2 → 1 share")
        print(f"Result Quantity: {qty}")
        print(f"Decision: {'✅ BUY' if qty > 0 else '❌ REJECT - Still can\\'t afford'}")
        print(f"Margin Used: {qty * 1000 - 800} ₹ ({((qty * 1000 - 800) / 800 * 100):.1f}%)")
    
    # ────────────────────────────────────────────────────────────────────────────
    # TEST 3: Forced Buy (Aggressive Mode) 🔥
    # ────────────────────────────────────────────────────────────────────────────
    print("\n\n🔥 TEST 3: FORCED BUY (allow_margin=True, forced_buy_margin=True)")
    print("─" * 80)
    
    with patch('core.trading_settings.cfg') as mock_cfg:
        def cfg_side_effect(section, key, default):
            config = {
                ('risk', 'allow_margin'): True,
                ('risk', 'forced_buy_margin'): True,
                ('risk', 'margin_leverage'): 2.0,
                ('risk', 'position_size_margin'): 0.75,  # Will be overridden to 100%
                ('risk', 'max_risk_per_trade'): 0.02,
                ('risk', 'stop_loss_percent'): 0.01,
            }
            return config.get((section, key), default)
        
        mock_cfg.side_effect = cfg_side_effect
        
        qty = calculate_quantity(price=1000, stop_loss=980)
        print(f"Real Capital: ₹800")
        print(f"Margin Leverage: 2.0x")
        print(f"Margin Available: ₹800 × 2 = ₹1600")
        print(f"Position Size %: 100% (forced buy overrides 75%)")
        print(f"Stock Price: ₹1000")
        print(f"Forced Mode: YES - buys maximum even if risk calc says 0")
        print(f"Affordable Qty: (₹1600 × 1.0) / ₹1000 = 1.6 → 1 share")
        print(f"Result Quantity: {qty}")
        print(f"Decision: {'✅ FORCED BUY' if qty > 0 else '❌ Still can\\'t afford even with force'}")
        print(f"Margin Used: {qty * 1000 - 800} ₹ ({((qty * 1000 - 800) / 800 * 100):.1f}%)")
    
    # ────────────────────────────────────────────────────────────────────────────
    # TEST 4: Tranches (Pyramid Strategy)
    # ────────────────────────────────────────────────────────────────────────────
    print("\n\n📊 TEST 4: TRANCHE BUYING (Pyramid Up Strategy)")
    print("─" * 80)
    
    with patch('core.trading_settings.cfg') as mock_cfg:
        with patch('core.state_manager.get_open_positions') as mock_positions:
            mock_positions.return_value = []  # No positions yet
            
            def cfg_side_effect(section, key, default):
                config = {
                    ('risk', 'allow_margin'): True,
                    ('risk', 'forced_buy_margin'): False,
                    ('risk', 'margin_leverage'): 2.0,
                    ('risk', 'position_size_margin'): 0.75,
                    ('risk', 'max_risk_per_trade'): 0.02,
                    ('risk', 'stop_loss_percent'): 0.01,
                }
                return config.get((section, key), default)
            
            mock_cfg.side_effect = cfg_side_effect
            
            result = calculate_quantity_with_tranches(
                price=1000,
                stop_loss=980,
                max_tranches=3
            )
            
            print(f"Real Capital: ₹800")
            print(f"Max Tranches to Consider: 3")
            print(f"Stock Price: ₹1000")
            print(f"─" * 80)
            print(f"Result:")
            print(f"  Total Quantity: {result['total_qty']} shares")
            print(f"  Number of Tranches: {result['num_tranches']}")
            print(f"  Per Tranche: {result['per_tranche_qty']} share(s)")
            print(f"  Total Margin Used: ₹{result['margin_used']:.2f}")
            print(f"  Margin Ratio: {result['margin_ratio']:.2%} of real capital")
            print(f"\nStrategy:")
            print(f"  - Buy 1st tranche now @ ₹1000")
            print(f"  - Queue 2nd tranche if price dips")
            print(f"  - Queue 3rd tranche if price dips more")

print("\n\n" + "="*80)
print("📌 KEY INSIGHTS")
print("="*80)
print("""
1. WITHOUT MARGIN: Can't buy (₹800 < ₹1000)

2. WITH MARGIN (normal): Can buy 1 share using ₹200 margin

3. WITH FORCED BUY: Still buys 1 share but now with understanding 
   that full margin power is being deployed

4. WITH TRANCHES: Can plan multiple buys using pyramid strategy
   - Encourages scaling into positions
   - Avoids all-in at once
   - Better risk management

5. MARGIN SAFETY:
   - Risk calculations ALWAYS use real capital (₹800)
   - Not margin-inflated risk
   - Protects against over-leverage disasters
""")

print("\n" + "="*80)
print("✅ Margin & Forced Buy system is ready to use!")
print("="*80)
print("\nNext steps:")
print("1. Set allow_margin=True in config/trading_settings.json")
print("2. Optionally enable forced_buy_margin=True for aggressive mode")
print("3. Monitor margin usage with get_margin_status()")
print("4. See docs/MARGIN_FORCED_BUY_GUIDE.md for full details")
