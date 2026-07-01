#!/usr/bin/env python3
"""
EMERGENCY SQUARE OFF ALL — Close every position immediately.
Called when system is shutting down or user presses emergency button.
"""
import logging
import os
from datetime import datetime
from dotenv import load_dotenv
from core.safe_io import safe_float, safe_int

load_dotenv()
logger = logging.getLogger(__name__)

TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")


def _get_exit_price_with_fallback(symbol: str, entry_price: float) -> tuple[float, str]:
    """
    Try to get current exit price with fallbacks.
    Returns (price, source) tuple.
    
    Fallback chain:
    1. WebSocket live tick (requires token-to-symbol mapping)
    2. yfinance current price (API fallback)
    3. Entry price (last resort with warning)
    """
    symbol_upper = str(symbol or "").strip().upper()
    
    # ─ Attempt 1: WebSocket live tick ─────────────────────
    try:
        from core.data_fetcher import get_latest_tick
        tick = get_latest_tick(symbol_upper)
        if tick:
            price = safe_float(tick.get("ltp"), 0.0)
            if price > 0:
                logger.info(f"  ✅ Price for {symbol_upper}: ₹{price} (ws_ltp)")
                return price, "ws_ltp"
        else:
            logger.debug(
                f"  ⚠️  WebSocket tick not found for {symbol_upper} — "
                f"(token mapping may not be populated yet)"
            )
    except Exception as e:
        logger.debug(f"WebSocket tick lookup failed for {symbol_upper}: {e}")
    
    # ─ Attempt 2: yfinance current price ─────────────────
    try:
        logger.debug(f"  Attempting yfinance lookup for {symbol_upper}...")
        import yfinance as yf
        nse_ticker = f"{symbol_upper}.NS"
        data = yf.Ticker(nse_ticker, session=None)
        if data and data.info:
            price = safe_float(data.info.get("currentPrice") or data.info.get("regularMarketPrice"), 0.0)
            if price > 0:
                logger.info(f"  ✅ Price for {symbol_upper}: ₹{price} (yfinance)")
                return price, "yfinance"
        logger.debug(f"  yfinance returned no price for {symbol_upper}")
    except Exception as e:
        logger.debug(f"yfinance lookup failed for {symbol_upper}: {e}")
    
    # ─ Attempt 3: Last resort — entry price with warning ──
    if entry_price > 0:
        logger.warning(
            f"  ⚠️  {symbol_upper}: Using entry price ₹{entry_price} as emergency fallback"
        )
        return entry_price, "entry_price_fallback"
    
    # ─ Complete failure ───────────────────────────────────
    logger.error(f"  ❌ Could not determine any price for {symbol_upper}")
    return 0.0, "no_price"


def emergency_square_off_all() -> dict:
    """
    🚨 EMERGENCY: Close every open position immediately at market price.
    
    Uses multi-level fallback for price discovery:
    1. WebSocket live tick (real-time)
    2. yfinance current price (API fallback)
    3. Entry price (last resort in emergency)

    Returns: {
        'status': 'SUCCESS' | 'PARTIAL' | 'FAILED',
        'closed_count': int,
        'failed_count': int,
        'details': [{symbol, qty, exit_price, status, error}],
        'timestamp': ISO string
    }
    """
    logger.critical("🚨 EMERGENCY SQUARE OFF ALL INITIATED")
    
    # ─ Diagnostic: Check token mapping status ──────────────
    try:
        from core.data_fetcher import _token_to_symbol, _latest_tick
        logger.info(
            f"  Token mapping status: {len(_token_to_symbol)} tokens mapped"
        )
        if _latest_tick:
            logger.info(
                f"  Live ticks cached: {list(_latest_tick.keys())}"
            )
        else:
            logger.warning(
                f"  ⚠️  No live ticks in cache! WebSocket may not be connected"
            )
    except Exception as e:
        logger.debug(f"Could not read token mapping: {e}")

    from core.state_manager import get_open_positions, lock_entries, mark_liquidating
    from core.order_executor import place_sell_order
    from core.audit_logger import audit_system_error

    mark_liquidating("EMERGENCY_SQUAREOFF_REQUESTED")
    positions = get_open_positions()
    if not positions:
        logger.info("No positions to close")
        lock_entries("EMERGENCY_SQUAREOFF_NO_OPEN_POSITIONS")
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
        qty = safe_int(pos.get("quantity", 0), 0)
        entry_price = safe_float(pos.get("entry_price", 0), 0.0)
        
        if qty <= 0:
            logger.error(f"  ❌ Invalid quantity for {symbol}: {pos.get('quantity')}")
            failed.append({
                'symbol': symbol,
                'qty': qty,
                'exit_price': 0,
                'status': 'FAILED',
                'error': 'Invalid quantity'
            })
            continue

        try:
            logger.warning(f"Closing {symbol} {qty} shares (entry: Rs{entry_price})")

            # Get exit price with fallbacks
            exit_price, exit_price_source = _get_exit_price_with_fallback(symbol, entry_price)
            
            if exit_price <= 0:
                logger.error(f"  ❌ No price available for {symbol}; skipping")
                failed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'exit_price': 0,
                    'exit_price_source': 'no_price',
                    'status': 'FAILED',
                    'error': 'Could not determine exit price'
                })
                continue

            # Execute SELL at market (or limit at fallback price)
            result = place_sell_order(
                symbol=symbol,
                exit_price=exit_price,
                reason="EMERGENCY_SQUAREOFF",
                exit_price_source=exit_price_source,
            )

            if result:
                closed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'exit_price': exit_price,
                    'exit_price_source': exit_price_source,
                    'status': 'CLOSED',
                    'error': None
                })
                logger.warning(f"  ✅ CLOSED at Rs{exit_price} ({exit_price_source})")
            else:
                failed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'exit_price': exit_price,
                    'exit_price_source': exit_price_source,
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

    # Build result dict with explicit int types for serialization
    closed_count = int(len(closed))
    failed_count = int(len(failed))
    
    result = {
        'status': overall_status,
        'closed_count': closed_count,
        'failed_count': failed_count,
        'details': closed + failed,
        'timestamp': datetime.now().isoformat(),
    }
    
    # Diagnostic logging before return
    logger.info(f"  Result dict structure: status={result['status']}, "
                f"closed_count={result['closed_count']} (type: {type(result['closed_count']).__name__}), "
                f"failed_count={result['failed_count']} (type: {type(result['failed_count']).__name__})")
    
    lock_entries(f"EMERGENCY_SQUAREOFF_{overall_status}")

    logger.critical(
        f"🚨 EMERGENCY SQUAREOFF COMPLETE: {closed_count} closed, {failed_count} failed"
    )

    return result


def trigger_emergency_squareoff():
    """Trigger emergency squareoff of all positions.

    P2-3 FIX: preserve the real SUCCESS/PARTIAL/FAILED outcome in the session
    reason instead of unconditionally overwriting it with TRIGGER_COMPLETE (which
    made the dashboard always report success). The final reason now reflects
    whether positions actually closed.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        from core.state_manager import get_open_positions, lock_entries, mark_liquidating

        mark_liquidating("EMERGENCY_SQUAREOFF_TRIGGERED")
        positions = get_open_positions()
        if not positions:
            lock_entries("EMERGENCY_SQUAREOFF_SUCCESS")
            logger.info("Emergency squareoff: no open positions")
            return True

        logger.warning(f"🚨 EMERGENCY SQUAREOFF: Closing {len(positions)} positions")
        result = emergency_square_off_all() or {}
        reported = str(result.get("status", "UNKNOWN")).upper()

        # Authoritative check: what actually remains open after the attempt.
        remaining = len(get_open_positions())
        if remaining == 0:
            final_status = "SUCCESS"
        elif reported in ("PARTIAL", "FAILED"):
            final_status = reported
        else:
            final_status = "PARTIAL"

        lock_entries(f"EMERGENCY_SQUAREOFF_{final_status}")
        logger.info(
            "✅ Emergency squareoff finished | reported=%s | remaining_open=%d | final=%s",
            reported, remaining, final_status,
        )
        return remaining == 0
    except Exception as e:
        logger.error(f"❌ Emergency squareoff failed: {e}")
        try:
            from core.state_manager import lock_entries
            lock_entries("EMERGENCY_SQUAREOFF_FAILED")
        except Exception:
            pass
        return False
