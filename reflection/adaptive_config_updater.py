# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/adaptive_config_updater.py — Automatic Config Generation
#
#   Reads reflection statistics and calculates adaptive multipliers.
#   Updates trading_settings.json with signal/time-window/symbol adjustments.
#
#   This enables automatic parameter tuning based on measured outcomes.
# ============================================================

import logging
import sqlite3
from pathlib import Path
from datetime import datetime

from core.safe_io import atomic_write_json, safe_float, safe_int, safe_read_json
from reflection.reflection_engine import (
    DB_PATH,
    get_all_signal_stats,
    get_all_time_window_stats,
    get_all_symbol_stats,
    get_confidence_multiplier,
    get_time_window_multiplier,
    get_symbol_sl_adjustment,
    _calculate_confidence_strength,
    _apply_ema_smoothing,
    _apply_daily_adjustment_limit,
    _get_historical_multiplier,
    _store_multiplier_history,
    _save_config_history,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config/trading_settings.json")
MULTIPLIER_FLOOR = 0.4
MULTIPLIER_CEILING = 1.2
SYMBOL_SL_MIN_TRADES = 10


# ════════════════════════════════════════════════════════════
#   LOAD & SAVE CONFIG
# ════════════════════════════════════════════════════════════

def _load_config() -> dict:
    """Load trading_settings.json."""
    return safe_read_json(
        CONFIG_PATH,
        {},
        expected_type=dict,
        label="adaptive trading settings",
        log=logger,
    )


def _save_config(config: dict) -> bool:
    """Save trading_settings.json."""
    try:
        if not atomic_write_json(CONFIG_PATH, config, label="adaptive trading settings", log=logger):
            return False
        logger.info(f"✅ Config saved: {CONFIG_PATH}")
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def _clamp_multiplier(value: float, floor: float = MULTIPLIER_FLOOR, ceiling: float = MULTIPLIER_CEILING) -> float:
    return max(floor, min(ceiling, safe_float(value, 1.0)))


def _symbol_sample_size(symbol: str) -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT COUNT(*) FROM trade_records WHERE symbol = ?", (symbol,))
        count = cur.fetchone()[0]
        conn.close()
        return safe_int(count, 0)
    except Exception as exc:
        logger.warning("Symbol sample lookup failed for %s: %s", symbol, exc)
        return 0


# ════════════════════════════════════════════════════════════
#   CALCULATE ADAPTIVE MULTIPLIERS
# ════════════════════════════════════════════════════════════

def _calculate_signal_multipliers() -> dict[str, float]:
    """
    Calculate signal confidence multipliers with smoothing and constraints.
    
    Process:
    1. Calculate raw multiplier from win rate
    2. Get historical multiplier (previous day)
    3. Calculate confidence strength from sample size
    4. Apply EMA smoothing to prevent overreaction
    5. Apply daily adjustment limit
    6. Store for next update
    
    Returns: { "signal_name": multiplier }
    """
    multipliers = {}
    
    signal_stats = get_all_signal_stats()
    
    for stats in signal_stats:
        signal_name = stats["signal_name"].lower().replace(" ", "_")
        
        # 1. Calculate raw multiplier
        raw_multiplier = get_confidence_multiplier(stats["signal_name"])
        
        # 2. Get historical multiplier
        historical_mult = _get_historical_multiplier("signal", signal_name)
        if historical_mult is None:
            historical_mult = 1.0  # Default on first run
        
        # 3. Calculate confidence strength from sample size
        confidence_strength = _calculate_confidence_strength(
            total_trades=stats["total_trades"],
            min_trades=10,
            target_trades=100,
        )
        
        # 4. Apply EMA smoothing
        smoothed_mult = _apply_ema_smoothing(
            old_multiplier=historical_mult,
            new_multiplier=raw_multiplier,
            smoothing_alpha=0.2,
            confidence_strength=confidence_strength,
        )
        
        # 5. Apply daily adjustment limit (max 5% change per day)
        final_mult = _apply_daily_adjustment_limit(
            old_multiplier=historical_mult,
            new_multiplier=smoothed_mult,
            max_daily_change_pct=5.0,
        )
        final_mult = _clamp_multiplier(final_mult)
        
        # 6. Store for next update
        _store_multiplier_history(
            multiplier_type="signal",
            multiplier_key=signal_name,
            multiplier_value=final_mult,
            raw_calculated_value=raw_multiplier,
            sample_size=stats["total_trades"],
            confidence_strength=confidence_strength,
        )
        
        multipliers[signal_name] = final_mult
        
        logger.debug(
            f"📊 Signal: {signal_name} | Trades: {stats['total_trades']} | "
            f"WR: {stats['win_rate']:.1f}% | Raw: {raw_multiplier:.2f} | "
            f"Confidence: {confidence_strength:.2f} | "
            f"Smoothed: {smoothed_mult:.2f} | Final: {final_mult:.2f}"
        )
    
    return multipliers


def _calculate_time_window_multipliers() -> dict[str, float]:
    """
    Calculate time window multipliers with smoothing and constraints.
    
    Process: Same as signal multipliers but per hourly window.
    
    Returns: { "9:15-10:00": multiplier }
    """
    multipliers = {}
    
    window_stats = get_all_time_window_stats()
    
    for stats in window_stats:
        time_window = stats["time_window"]
        
        # 1. Calculate raw multiplier
        raw_multiplier = get_time_window_multiplier(time_window)
        
        # 2. Get historical multiplier
        historical_mult = _get_historical_multiplier("time_window", time_window)
        if historical_mult is None:
            historical_mult = 1.0
        
        # 3. Calculate confidence strength
        confidence_strength = _calculate_confidence_strength(
            total_trades=stats["trade_count"],
            min_trades=20,
            target_trades=100,
        )
        
        # 4. Apply EMA smoothing
        smoothed_mult = _apply_ema_smoothing(
            old_multiplier=historical_mult,
            new_multiplier=raw_multiplier,
            smoothing_alpha=0.2,
            confidence_strength=confidence_strength,
        )
        
        # 5. Apply daily adjustment limit
        final_mult = _apply_daily_adjustment_limit(
            old_multiplier=historical_mult,
            new_multiplier=smoothed_mult,
            max_daily_change_pct=5.0,
        )
        final_mult = _clamp_multiplier(final_mult)
        
        # 6. Store for next update
        _store_multiplier_history(
            multiplier_type="time_window",
            multiplier_key=time_window,
            multiplier_value=final_mult,
            raw_calculated_value=raw_multiplier,
            sample_size=stats["trade_count"],
            confidence_strength=confidence_strength,
        )
        
        multipliers[time_window] = final_mult
        
        logger.debug(
            f"⏰ Window: {time_window} | Trades: {stats['trade_count']} | "
            f"WR: {stats['win_rate']:.1f}% | Raw: {raw_multiplier:.2f} | "
            f"Confidence: {confidence_strength:.2f} | Final: {final_mult:.2f}"
        )
    
    return multipliers


def _calculate_symbol_sl_adjustments() -> dict[str, float]:
    """
    Calculate per-symbol stop loss adjustments with smoothing and constraints.
    
    Process: Same as signals but applied per-symbol based on volatility.
    
    Returns: { "NTPC": 0.8, "RELIANCE": 1.1 }
    Lower value = tighter SL (less risk)
    Higher value = wider SL (more risk tolerance)
    """
    adjustments = {}
    
    symbol_stats = get_all_symbol_stats()
    
    for stats in symbol_stats:
        symbol = stats["symbol"]
        sample_size = _symbol_sample_size(symbol)
        
        # 1. Calculate raw adjustment
        raw_adjustment = get_symbol_sl_adjustment(symbol)
        
        # 2. Get historical adjustment
        historical_adj = _get_historical_multiplier("symbol_sl", symbol)
        if historical_adj is None:
            historical_adj = 1.0
        
        confidence_strength = _calculate_confidence_strength(
            total_trades=sample_size,
            min_trades=SYMBOL_SL_MIN_TRADES,
            target_trades=100,
        )
        
        # 4. Apply EMA smoothing
        smoothed_adj = _apply_ema_smoothing(
            old_multiplier=historical_adj,
            new_multiplier=raw_adjustment,
            smoothing_alpha=0.2,
            confidence_strength=confidence_strength,
        )
        
        # 5. Apply daily adjustment limit (max 3% for SL to avoid whipsaws)
        final_adj = _apply_daily_adjustment_limit(
            old_multiplier=historical_adj,
            new_multiplier=smoothed_adj,
            max_daily_change_pct=3.0,  # Tighter than signals
        )
        final_adj = _clamp_multiplier(final_adj, floor=0.5, ceiling=2.0)
        
        # 6. Store for next update
        _store_multiplier_history(
            multiplier_type="symbol_sl",
            multiplier_key=symbol,
            multiplier_value=final_adj,
            raw_calculated_value=raw_adjustment,
            sample_size=sample_size,
            confidence_strength=confidence_strength,
        )
        
        adjustments[symbol] = final_adj
        
        logger.debug(
            f"📈 Symbol: {symbol} | Trades: {sample_size} | Volatility: {stats['volatility_profile']} | "
            f"DD: {stats['avg_drawdown']:.2f}% | Raw: {raw_adjustment:.2f} | "
            f"Final: {final_adj:.2f}"
        )
    
    return adjustments


def _calculate_market_regime_multiplier() -> float:
    """
    Calculate overall market regime multiplier.
    
    Based on overall win rate across all trades.
    """
    signal_stats = get_all_signal_stats()
    
    if not signal_stats:
        return 1.0
    
    total_trades = sum(s["total_trades"] for s in signal_stats)
    total_wins = sum(s["winning_trades"] for s in signal_stats)
    
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 50.0
    
    # 1. Calculate raw market regime multiplier based on overall win rate
    if overall_wr < 45:
        raw_multiplier = 0.8
    elif overall_wr < 50:
        raw_multiplier = 0.9
    elif overall_wr < 55:
        raw_multiplier = 1.0
    elif overall_wr < 60:
        raw_multiplier = 1.05
    else:
        raw_multiplier = 1.1
    
    # 2. Get historical multiplier
    historical_mult = _get_historical_multiplier("market_regime", "overall")
    if historical_mult is None:
        historical_mult = 1.0
    
    # 3. Calculate confidence strength based on total sample size
    confidence_strength = _calculate_confidence_strength(
        total_trades=total_trades,
        min_trades=50,
        target_trades=500,
    )
    
    # 4. Apply EMA smoothing
    smoothed_mult = _apply_ema_smoothing(
        old_multiplier=historical_mult,
        new_multiplier=raw_multiplier,
        smoothing_alpha=0.2,
        confidence_strength=confidence_strength,
    )
    
    # 5. Apply daily adjustment limit (max 5% for market-wide multiplier)
    final_mult = _apply_daily_adjustment_limit(
        old_multiplier=historical_mult,
        new_multiplier=smoothed_mult,
        max_daily_change_pct=5.0,
    )
    final_mult = _clamp_multiplier(final_mult)
    
    # 6. Store for next update
    _store_multiplier_history(
        multiplier_type="market_regime",
        multiplier_key="overall",
        multiplier_value=final_mult,
        raw_calculated_value=raw_multiplier,
        sample_size=total_trades,
        confidence_strength=confidence_strength,
    )
    
    logger.debug(
        f"🌍 Market Regime | Overall WR: {overall_wr:.1f}% ({total_wins}/{total_trades}) | "
        f"Raw: {raw_multiplier:.2f} | Confidence: {confidence_strength:.2f} | "
        f"Smoothed: {smoothed_mult:.2f} | Final: {final_mult:.2f}"
    )
    
    return final_mult


# ════════════════════════════════════════════════════════════
#   UPDATE CONFIG
# ════════════════════════════════════════════════════════════

def apply_adaptive_config():
    """
    Calculate adaptive multipliers and update trading_settings.json.
    """
    try:
        logger.info("🔧 Calculating adaptive configuration...")
        
        # Calculate multipliers
        signal_multipliers = _calculate_signal_multipliers()
        time_window_multipliers = _calculate_time_window_multipliers()
        symbol_sl_adjustments = _calculate_symbol_sl_adjustments()
        market_regime_multiplier = _calculate_market_regime_multiplier()
        
        # Load current config
        config = _load_config()
        
        # Create or update adaptive section
        if "adaptive" not in config:
            config["adaptive"] = {}
        
        # Update adaptive config
        config["adaptive"] = {
            "last_updated": datetime.now().isoformat(),
            "strategy": {
                "signal_confidence_multipliers": signal_multipliers,
                "market_regime_multiplier": market_regime_multiplier,
            },
            "time_windows": time_window_multipliers,
            "symbol_stops": symbol_sl_adjustments,
        }
        
        # Save config
        if _save_config(config):
            # Save to config history for auditing
            changes_made = (
                f"Signals: {len(signal_multipliers)}, "
                f"Windows: {len(time_window_multipliers)}, "
                f"Symbols: {len(symbol_sl_adjustments)}, "
                f"Market Regime: {market_regime_multiplier:.2f}"
            )
            _save_config_history(config.get("adaptive", {}), changes_made)
            
            logger.info(
                f"✅ Adaptive config updated | "
                f"Signals: {len(signal_multipliers)} | "
                f"Windows: {len(time_window_multipliers)} | "
                f"Symbols: {len(symbol_sl_adjustments)} | "
                f"Market Regime: {market_regime_multiplier:.2f}"
            )
            return True
        else:
            logger.error("Failed to save adaptive config")
            return False
            
    except Exception as e:
        logger.error(f"Failed to apply adaptive config: {e}", exc_info=True)
        return False


def get_adaptive_config_summary() -> dict:
    """Get current adaptive configuration summary."""
    try:
        config = _load_config()
        
        if "adaptive" not in config:
            return {}
        
        adaptive = config["adaptive"]
        
        return {
            "last_updated": adaptive.get("last_updated", "Never"),
            "signal_count": len(adaptive.get("strategy", {}).get("signal_confidence_multipliers", {})),
            "time_window_count": len(adaptive.get("time_windows", {})),
            "symbol_count": len(adaptive.get("symbol_stops", {})),
            "market_regime_multiplier": adaptive.get("strategy", {}).get("market_regime_multiplier", 1.0),
        }
    except Exception as e:
        logger.error(f"Failed to get adaptive config summary: {e}")
        return {}


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    apply_adaptive_config()
    print(get_adaptive_config_summary())
