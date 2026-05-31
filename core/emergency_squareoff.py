#!/usr/bin/env python3
"""
EMERGENCY SQUARE OFF ALL — Close every position immediately.
Called when system is shutting down or user presses emergency button.
"""
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")


async def emergency_square_off_all() -> dict:
    """
    🚨 EMERGENCY: Close every open position immediately at market price.

    Returns: {
        'status': 'SUCCESS' | 'PARTIAL' | 'FAILED',
        'closed_count': int,
        'failed_count': int,
        'details': [{symbol, qty, exit_price, status, error}],
        'timestamp': ISO string
    }
    """
    logger.critical("🚨 EMERGENCY SQUARE OFF ALL INITIATED")

    from core.state_manager import get_open_positions
    from core.data_fetcher import get_latest_tick
    from core.order_executor import place_sell_order
    from core.audit_logger import audit_system_error

    positions = get_open_positions()
    if not positions:
        logger.info("No positions to close")
        return {
            'status': 'SUCCESS',
            'closed_count': 0,
            'failed_count': 0,
            'details': [],
            'timestamp': datetime.now().isoformat(),
        }

    closed = []
    failed = []

    for pos in positions:
        symbol = pos.get("symbol", "")
        qty = int(pos.get("quantity", 0))
        entry_price = float(pos.get("entry_price", 0))

        try:
            logger.warning(f"Closing {symbol} {qty} shares (entry: Rs{entry_price})")

            # Get current price
            tick = get_latest_tick(symbol)
            if tick:
                exit_price = float(tick.get("ltp", entry_price))
            else:
                exit_price = entry_price
                logger.warning(f"  No tick for {symbol}, using entry price Rs{exit_price}")

            # Execute SELL at market
            result = place_sell_order(
                symbol=symbol,
                exit_price=exit_price,
                reason="EMERGENCY_SQUAREOFF",
            )

            if result:
                closed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'exit_price': exit_price,
                    'status': 'CLOSED',
                    'error': None
                })
                logger.warning(f"  ✅ CLOSED at Rs{exit_price}")
            else:
                failed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'exit_price': exit_price,
                    'status': 'FAILED',
                    'error': 'SELL execution returned False'
                })
                logger.error(f"  ❌ SELL FAILED for {symbol}")

        except Exception as e:
            logger.error(f"  ❌ Exception closing {symbol}: {e}", exc_info=True)
            audit_system_error(f"Emergency squareoff failed for {symbol}: {e}")
            failed.append({
                'symbol': symbol,
                'qty': qty,
                'exit_price': 0,
                'status': 'FAILED',
                'error': str(e)
            })

    # Determine overall status
    if len(failed) == 0:
        overall_status = 'SUCCESS'
    elif len(closed) > 0:
        overall_status = 'PARTIAL'
    else:
        overall_status = 'FAILED'

    result = {
        'status': overall_status,
        'closed_count': len(closed),
        'failed_count': len(failed),
        'details': closed + failed,
        'timestamp': datetime.now().isoformat(),
    }

    logger.critical(
        f"🚨 EMERGENCY SQUAREOFF COMPLETE: {len(closed)} closed, {len(failed)} failed"
    )

    return result
