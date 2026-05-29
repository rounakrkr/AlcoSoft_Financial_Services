#!/usr/bin/env python
"""Test SL order placement to diagnose failures."""
import sys
import logging
from core.order_executor import place_buy_order

# Setup logging to see detailed messages
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

# Test with a simple order
result = place_buy_order(
    symbol='TEST',
    trading_symbol='SBIN-EQ',
    entry_price=500.0,
    stop_loss=495.0,
    strategy='TEST'
)
print(f"\n✅ Order placed: {result}")
