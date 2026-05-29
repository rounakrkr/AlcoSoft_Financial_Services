# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/adaptive_config.py
#
#   Adaptive Configuration Generator
#   Generates trading_settings.json updates based on:
#   - Signal performance
#   - Time window analysis
#   - Per-symbol behavior
#   - Market conditions
#
#   NOT a trading system — only generates config suggestions.
# ============================================================

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional

from reflection.reflection_statistics import (
    get_signal_win_rates,
    get_time_window_analysis,
    get_symbol_analysis,
    get_latest_market_observation,
    record_adaptive_config_history,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config/trading_settings.json")
MIN_SAMPLE_SIZE = 5  # Minimum trades to consider


# ════════════════════════════════════════════════════════════
#   ADAPTIVE SIGNAL CONFIDENCE
# ════════════════════════════════════════════════════════════

def _calculate_signal_confidence_multipliers(lookback_days: int = 30) -> Dict[str, float]:
    """
    Calculate confidence multipliers for each signal based on win rate.
    
    Formula:
    - High performers (wr >= 60%): multiplier = 1.2
    - Normal (45-60%): multiplier = 1.0
    - Weak (< 45%): multiplier = 0.6
    """
    multipliers = {}
    signal_stats = get_signal_win_rates()
    
    for signal_name, stats in signal_stats.items():
        total = stats["total_trades"]
        
        # Only consider signals with minimum sample size
        if total < MIN_SAMPLE_SIZE:
            continue
        
        wr = stats["win_rate"]
        
        if wr >= 60:
            multipliers[signal_name.lower().replace(" ", "_")] = 1.2
        elif wr >= 45:
            multipliers[signal_name.lower().replace(" ", "_")] = 1.0
        else:
            multipliers[signal_name.lower().replace(" ", "_")] = 0.6
        
        logger.debug(f"Signal confidence: {signal_name} | WR={wr}% → {multipliers[signal_name.lower()]}")
    
    return multipliers


# ════════════════════════════════════════════════════════════
#   ADAPTIVE TIME WINDOW MULTIPLIERS
# ════════════════════════════════════════════════════════════

def _calculate_time_window_multipliers() -> Dict[str, float]:
    """
    Calculate confidence multipliers for each time window.
    
    Logic:
    - Strong windows (wr >= 55%): 1.0 (normal)
    - Weak windows (wr < 45%): 0.4 (reduced)
    - Avoid windows with high fakeout rates
    """
    multipliers = {}
    time_stats = get_time_window_analysis()
    
    for time_window, stats in time_stats.items():
        wr = stats["win_rate"]
        fakeouts = stats["avg_fakeouts"]
        
        if wr >= 55 and fakeouts < 2:
            multipliers[time_window] = 1.0
        elif wr >= 45:
            multipliers[time_window] = 0.7
        else:
            multipliers[time_window] = 0.4
        
        logger.debug(
            f"Time window: {time_window} | WR={wr}% | Fakeouts={fakeouts} → {multipliers[time_window]}"
        )
    
    return multipliers


# ════════════════════════════════════════════════════════════
#   ADAPTIVE STOP LOSS RECOMMENDATIONS
# ════════════════════════════════════════════════════════════

def _calculate_adaptive_sl_percentages() -> Dict[str, float]:
    """
    Calculate recommended SL percentages per symbol.
    
    Formula:
    - recommended_sl = avg_drawdown * 1.2 (20% safety buffer)
    - Capped between 0.3% and 2.0%
    """
    sls = {}
    symbol_stats = get_symbol_analysis()
    
    for symbol, stats in symbol_stats.items():
        if stats["trade_count"] < MIN_SAMPLE_SIZE:
            continue
        
        avg_dd = stats["avg_drawdown"]
        rec_sl = min(max(avg_dd * 1.2, 0.3), 2.0)
        
        sls[symbol] = round(rec_sl, 2)
        
        logger.debug(
            f"SL recommendation: {symbol} | "
            f"AvgDD={avg_dd}% → SL={rec_sl}%"
        )
    
    return sls


# ════════════════════════════════════════════════════════════
#   ADAPTIVE MARKET REGIME MULTIPLIER
# ════════════════════════════════════════════════════════════

def _calculate_market_regime_multiplier() -> float:
    """
    Calculate overall market regime confidence multiplier.
    
    Based on latest market observation:
    - Bullish + Strong Trend: 1.1
    - Bearish or Mixed: 0.8
    - Unknown: 1.0
    """
    obs = get_latest_market_observation()
    if not obs:
        return 1.0
    
    market_cond = obs.get("market_condition", "UNKNOWN")
    trend = obs.get("trend_strength", "UNKNOWN")
    
    if market_cond == "BULLISH" and trend == "STRONG":
        return 1.1
    elif market_cond == "BEARISH":
        return 0.8
    elif market_cond in ("MIXED", "RANGING"):
        return 0.85
    else:
        return 1.0


# ════════════════════════════════════════════════════════════
#   VOLATILITY-BASED FILTERS
# ════════════════════════════════════════════════════════════

def _calculate_volatility_filters() -> Dict[str, float]:
    """
    Generate volatility-based position sizing multipliers.
    
    Low volatility stocks → allow higher confidence
    High volatility stocks → reduce confidence
    """
    filters = {}
    symbol_stats = get_symbol_analysis()
    
    volatilities = [s["volatility"] for s in symbol_stats.values()]
    if not volatilities:
        return {}
    
    avg_vol = sum(volatilities) / len(volatilities)
    
    for symbol, stats in symbol_stats.items():
        vol = stats["volatility"]
        
        if vol <= avg_vol * 0.7:
            filters[symbol] = 1.1  # Low vol, higher confidence
        elif vol <= avg_vol:
            filters[symbol] = 1.0
        elif vol <= avg_vol * 1.3:
            filters[symbol] = 0.9
        else:
            filters[symbol] = 0.7  # High vol, lower confidence
    
    return filters


# ════════════════════════════════════════════════════════════
#   CONFIG GENERATION
# ════════════════════════════════════════════════════════════

def generate_adaptive_config() -> Dict:
    """
    Generate complete adaptive configuration.
    """
    logger.info("🔧 Generating adaptive configuration...")
    
    config = {
        "strategy": {
            "signal_confidence_multipliers": _calculate_signal_confidence_multipliers(),
            "market_regime_multiplier": _calculate_market_regime_multiplier(),
        },
        "time_windows": _calculate_time_window_multipliers(),
        "symbol_stops": _calculate_adaptive_sl_percentages(),
        "volatility_filters": _calculate_volatility_filters(),
        "generated_at": datetime.now().isoformat(),
        "notes": "Auto-generated adaptive config. Do not edit manually.",
    }
    
    return config


def load_current_trading_settings() -> Dict:
    """Load current trading_settings.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading trading settings: {e}")
        return {}


def merge_adaptive_config(base_config: Dict, adaptive_values: Dict) -> Dict:
    """
    Merge adaptive values into base configuration.
    Preserves base config structure, adds/updates adaptive sections.
    """
    merged = base_config.copy()
    
    # Add/update adaptive sections
    if "adaptive" not in merged:
        merged["adaptive"] = {}
    
    merged["adaptive"].update(adaptive_values)
    merged["last_adaptive_update"] = datetime.now().isoformat()
    
    return merged


def apply_adaptive_config(dry_run: bool = False) -> bool:
    """
    Generate and apply adaptive configuration.
    
    Args:
        dry_run: If True, only log changes without writing
    
    Returns:
        True if successful
    """
    try:
        # 1. Generate adaptive config
        adaptive = generate_adaptive_config()
        
        # 2. Load current settings
        current = load_current_trading_settings()
        
        # 3. Merge
        merged = merge_adaptive_config(current, adaptive)
        
        # 4. Log changes
        logger.info(
            f"📝 Adaptive config generated:\n"
            f"   Signal multipliers: {len(adaptive['strategy']['signal_confidence_multipliers'])} signals\n"
            f"   Time windows: {len(adaptive['time_windows'])} windows\n"
            f"   SL recommendations: {len(adaptive['symbol_stops'])} symbols\n"
            f"   Market multiplier: {adaptive['strategy']['market_regime_multiplier']}"
        )
        
        # 5. Write if not dry run
        if not dry_run:
            with open(CONFIG_PATH, "w") as f:
                json.dump(merged, f, indent=2)
            
            # Record in history
            try:
                record_adaptive_config_history(
                    config_json=json.dumps(adaptive),
                    reason="Automatic adaptive update based on signal performance",
                )
            except Exception as e:
                logger.warning(f"Could not record config history: {e}")
            
            logger.info(f"✅ Configuration updated: {CONFIG_PATH}")
        else:
            logger.info("(Dry run — no changes written)")
        
        return True
        
    except Exception as e:
        logger.error(f"Error applying adaptive config: {e}", exc_info=True)
        return False


# ════════════════════════════════════════════════════════════
#   CONFIG SUMMARY
# ════════════════════════════════════════════════════════════

def print_adaptive_config_summary():
    """Print human-readable summary of current adaptive config."""
    try:
        settings = load_current_trading_settings()
        adaptive = settings.get("adaptive", {})
        
        print("\n" + "="*60)
        print("ADAPTIVE CONFIGURATION SUMMARY")
        print("="*60)
        
        # Signal confidence
        multipliers = adaptive.get("strategy", {}).get("signal_confidence_multipliers", {})
        if multipliers:
            print("\n📊 SIGNAL CONFIDENCE MULTIPLIERS:")
            for sig, mult in multipliers.items():
                print(f"  {sig}: {mult}")
        
        # Time windows
        time_windows = adaptive.get("time_windows", {})
        if time_windows:
            print("\n⏰ TIME WINDOW MULTIPLIERS:")
            for window, mult in time_windows.items():
                print(f"  {window}: {mult}")
        
        # SL recommendations
        stops = adaptive.get("symbol_stops", {})
        if stops:
            print("\n🛑 STOP LOSS RECOMMENDATIONS (%):")
            for sym, sl in stops.items():
                print(f"  {sym}: {sl}%")
        
        # Market multiplier
        market_mult = adaptive.get("strategy", {}).get("market_regime_multiplier", 1.0)
        print(f"\n🌍 MARKET REGIME MULTIPLIER: {market_mult}")
        
        print("\n" + "="*60)
        
    except Exception as e:
        logger.error(f"Error printing summary: {e}")


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    apply_adaptive_config(dry_run=True)
    print_adaptive_config_summary()
