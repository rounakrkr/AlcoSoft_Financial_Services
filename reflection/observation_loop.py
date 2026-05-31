# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/observation_loop.py
#
#   Continuous Market Observation System
#   Runs periodically (e.g., every 15 minutes)
#
#   Responsibilities:
#   - Fetch current market snapshot
#   - Analyze trading behavior
#   - Measure signal reliability in real-time
#   - Generate market condition estimates
#   - Store observations for reflection
#
#   Does NOT:
#   - Place trades
#   - Create trading signals
#   - Override strategy decisions
# ============================================================

import asyncio
import logging
import json
from datetime import datetime, time as dt_time
from pathlib import Path
import pandas as pd

from core.data_fetcher import get_latest_tick, get_candle_history, get_feed_stats
from core.state_manager import get_open_positions, load_briefing
from reflection.reflection_statistics import (
    record_market_observation,
    get_signal_win_rates,
    get_time_window_analysis,
    get_symbol_analysis,
)

logger = logging.getLogger(__name__)

OBSERVATION_LOG_DIR = Path("data/observations")
OBSERVATION_LOG_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════
#   MARKET SNAPSHOT ANALYSIS
# ════════════════════════════════════════════════════════════

def _get_market_snapshot() -> dict:
    """
    Fetch current market snapshot.
    Returns: Dictionary with price data, volume data, and trends.
    """
    briefing = load_briefing()
    if not briefing:
        return {}
    
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "symbols_observed": 0,
        "symbols_with_data": 0,
        "avg_volume_percentile": 0,
        "price_movements": [],
    }
    
    # Get all stocks being monitored
    all_stocks = briefing.get("approved_stocks", []) + briefing.get("watchlist", [])
    snapshot["symbols_observed"] = len(all_stocks)
    
    volume_percentiles = []
    
    for stock in all_stocks[:10]:  # Sample first 10
        symbol = stock["ticker"]
        tick = get_latest_tick(symbol)
        
        if not tick:
            continue
        
        snapshot["symbols_with_data"] += 1
        
        # Get candle history for volume analysis
        candles = get_candle_history(symbol)
        if len(candles) >= 20:
            volumes = [c.get("volume", 0) for c in candles[-20:]]
            avg_vol_20 = sum(volumes) / len(volumes)
            current_vol = candles[-1].get("volume", 0)
            vol_percentile = (current_vol / avg_vol_20 * 100) if avg_vol_20 > 0 else 100
            volume_percentiles.append(vol_percentile)
            
            # Track price movements
            if len(candles) >= 2:
                prev_close = candles[-2].get("close", 0)
                curr_close = candles[-1].get("close", 0)
                if prev_close > 0:
                    pct_change = ((curr_close - prev_close) / prev_close) * 100
                    snapshot["price_movements"].append({
                        "symbol": symbol,
                        "pct_change": round(pct_change, 2),
                        "volume_percentile": round(vol_percentile, 1),
                    })
    
    if volume_percentiles:
        snapshot["avg_volume_percentile"] = round(sum(volume_percentiles) / len(volume_percentiles), 1)
    
    return snapshot


def _detect_market_condition(snapshot: dict) -> str:
    """
    Detect current market condition based on snapshot.
    Returns: "BULLISH", "BEARISH", "MIXED", or "RANGING"
    """
    if not snapshot.get("price_movements"):
        return "RANGING" if snapshot.get("symbols_with_data", 0) > 0 else "UNKNOWN"
    
    movements = snapshot["price_movements"]
    positive = sum(1 for m in movements if m["pct_change"] > 0)
    negative = sum(1 for m in movements if m["pct_change"] < 0)
    
    total = len(movements)
    positive_pct = (positive / total * 100) if total > 0 else 50
    
    if positive_pct >= 65:
        return "BULLISH"
    elif positive_pct <= 35:
        return "BEARISH"
    elif abs(positive_pct - 50) <= 10:
        return "RANGING"
    else:
        return "MIXED"


def _detect_trend_strength(snapshot: dict) -> str:
    """
    Detect trend strength.
    Returns: "STRONG", "WEAK", or "UNKNOWN"
    """
    if not snapshot.get("price_movements"):
        return "WEAK" if snapshot.get("symbols_with_data", 0) > 0 else "UNKNOWN"
    
    movements = snapshot["price_movements"]
    large_moves = sum(1 for m in movements if abs(m["pct_change"]) > 0.5)
    
    large_move_ratio = (large_moves / len(movements)) if movements else 0
    
    if large_move_ratio >= 0.6:
        return "STRONG"
    elif large_move_ratio >= 0.3:
        return "MODERATE"
    else:
        return "WEAK"


def _detect_volatility_regime(snapshot: dict) -> str:
    """
    Detect volatility regime.
    Returns: "HIGH", "NORMAL", "LOW"
    """
    vol_percentile = snapshot.get("avg_volume_percentile", 100)
    
    if vol_percentile >= 120:
        return "HIGH"
    elif vol_percentile >= 85:
        return "NORMAL"
    else:
        return "LOW"


def _analyze_breakout_activity(snapshot: dict) -> str:
    """
    Estimate breakout frequency based on volume.
    Returns: "HIGH", "NORMAL", "LOW"
    """
    vol_percentile = snapshot.get("avg_volume_percentile", 100)
    
    if vol_percentile >= 150:
        return "HIGH"
    elif vol_percentile >= 100:
        return "NORMAL"
    else:
        return "LOW"


def _analyze_reversal_activity() -> str:
    """
    Estimate reversal frequency based on recent signal data.
    Returns: "HIGH", "NORMAL", "LOW"
    """
    # Get recent win rates
    signal_stats = get_signal_win_rates()
    
    # Count strategies with low win rates (< 45%)
    weak_signals = sum(1 for s in signal_stats.values() if s["win_rate"] < 45)
    total_signals = len(signal_stats)
    
    if total_signals == 0:
        return "LOW"
    
    weak_ratio = weak_signals / total_signals
    
    if weak_ratio >= 0.6:
        return "HIGH"
    elif weak_ratio >= 0.3:
        return "NORMAL"
    else:
        return "LOW"


# ════════════════════════════════════════════════════════════
#   OBSERVATION COMPILATION
# ════════════════════════════════════════════════════════════

def _generate_observation_details(snapshot: dict) -> dict:
    """
    Generate detailed observation data.
    """
    return {
        "symbols_active": snapshot.get("symbols_with_data", 0),
        "market_snapshot": {
            "volume_percentile": snapshot.get("avg_volume_percentile", 0),
            "top_gainers": sorted(
                snapshot.get("price_movements", []),
                key=lambda x: x["pct_change"],
                reverse=True
            )[:3],
            "top_losers": sorted(
                snapshot.get("price_movements", []),
                key=lambda x: x["pct_change"]
            )[:3],
        },
        "signal_performance": get_signal_win_rates(),
        "time_window_stats": get_time_window_analysis(),
        "symbol_behavior": get_symbol_analysis(),
    }


async def run_observation_cycle():
    """
    Single observation cycle.
    - Fetch market snapshot
    - Analyze conditions
    - Generate observations
    - Store for later reflection
    - ALSO: Trigger cognitive observation cycle (if time matches)

    IMPORTANT: Market-tied cognition runs until 3:15 PM
    even after execution stops at 3:00 PM.
    """
    try:
        logger.info("🔍 Running observation cycle...")

        # ─ INTEGRATION: Trigger cognitive agents (every 15 min, 9:30 AM - 3:15 PM)
        try:
            from reflection.cognition_scheduler import schedule_cognitive_cycle
            schedule_cognitive_cycle()
        except Exception as e:
            logger.warning(f"Cognitive cycle scheduling failed (non-critical): {e}")

        # 1. Get market snapshot
        snapshot = _get_market_snapshot()
        
        if not snapshot.get("symbols_with_data"):
            logger.warning("No market data available for observation")
            return
        
        # 2. Analyze conditions
        market_condition = _detect_market_condition(snapshot)
        trend_strength = _detect_trend_strength(snapshot)
        volatility_regime = _detect_volatility_regime(snapshot)
        breakout_frequency = _analyze_breakout_activity(snapshot)
        reversal_frequency = _analyze_reversal_activity()
        
        # 3. Generate details
        details = _generate_observation_details(snapshot)
        
        # 4. Record observation
        record_market_observation(
            market_condition=market_condition,
            trend_strength=trend_strength,
            volatility_regime=volatility_regime,
            breakout_frequency=breakout_frequency,
            reversal_frequency=reversal_frequency,
            fake_signal_count=0,
            observations_json=details,
        )
        
        # 5. Log to file
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "market_condition": market_condition,
            "trend_strength": trend_strength,
            "volatility_regime": volatility_regime,
            "breakout_frequency": breakout_frequency,
            "reversal_frequency": reversal_frequency,
            "details": details,
        }
        
        log_file = OBSERVATION_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        
        logger.info(
            f"📊 Observation recorded:\n"
            f"   Market: {market_condition} | Trend: {trend_strength}\n"
            f"   Vol: {volatility_regime} | Breakouts: {breakout_frequency}\n"
            f"   Reversals: {reversal_frequency}"
        )
        
    except Exception as e:
        logger.error(f"Error in observation cycle: {e}", exc_info=True)


async def observation_loop_main(shutdown_event: asyncio.Event, interval_seconds: int = 900):
    """
    Main observation loop.
    Runs every N seconds (default 15 minutes).
    """
    logger.info(
        f"🔁 Observation loop started | "
        f"Interval: {interval_seconds}s ({interval_seconds//60} mins)"
    )
    
    while not shutdown_event.is_set():
        try:
            # Run observation cycle
            await run_observation_cycle()
            
            # Wait for next cycle
            await asyncio.sleep(interval_seconds)
            
        except asyncio.CancelledError:
            logger.info("Observation loop cancelled")
            break
        except Exception as e:
            logger.error(f"Observation loop error: {e}", exc_info=True)
            await asyncio.sleep(interval_seconds)
    
    logger.info("Observation loop stopped")


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/Extra Programs/Files/AlcoSoft_Financial_Services")
    
    # Test observation cycle
    asyncio.run(run_observation_cycle())
