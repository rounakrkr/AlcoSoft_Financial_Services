# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/reflection_engine.py — Statistical Performance Tracking
#
#   Persistent long-term memory of signal, time-window, and symbol performance.
#   Accumulates data across many trading days to enable adaptive behavior.
#
#   NOT a daily journal — this is MACHINE MEMORY for learning.
# ============================================================

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import math

logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path("data/reflection.db")
SNAPSHOTS_DIR = Path("data/reflection_snapshots")
DB_PATH.parent.mkdir(exist_ok=True)
SNAPSHOTS_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════
#   DATABASE INITIALIZATION
# ════════════════════════════════════════════════════════════

def _init_db():
    """Create tables if not exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Signal performance tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_name TEXT UNIQUE NOT NULL,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            losing_trades INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.0,
            avg_profit REAL DEFAULT 0.0,
            avg_loss REAL DEFAULT 0.0,
            avg_rr REAL DEFAULT 0.0,
            avg_drawdown REAL DEFAULT 0.0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Time window performance tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS time_window_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_window TEXT UNIQUE NOT NULL,
            trade_count INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.0,
            avg_pnl REAL DEFAULT 0.0,
            failure_rate REAL DEFAULT 0.0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Symbol behavior tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS symbol_behavior (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT UNIQUE NOT NULL,
            sl_hit_freq REAL DEFAULT 0.0,
            recovery_prob REAL DEFAULT 0.0,
            avg_drawdown REAL DEFAULT 0.0,
            volatility_profile TEXT DEFAULT 'NORMAL',
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Trade records (for historical analysis)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            pnl REAL NOT NULL,
            confidence REAL NOT NULL,
            time_window TEXT NOT NULL,
            drawdown REAL DEFAULT 0.0,
            sl_hit BOOLEAN DEFAULT 0,
            recovered BOOLEAN DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Historical multipliers for EMA smoothing
    c.execute("""
        CREATE TABLE IF NOT EXISTS multiplier_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            multiplier_type TEXT NOT NULL,
            multiplier_key TEXT NOT NULL,
            multiplier_value REAL NOT NULL,
            raw_calculated_value REAL NOT NULL,
            sample_size INTEGER DEFAULT 0,
            confidence_strength REAL DEFAULT 0.0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(multiplier_type, multiplier_key)
        )
    """)

    # Multiplier change log for audit and history tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS multiplier_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            multiplier_type TEXT NOT NULL,
            multiplier_key TEXT NOT NULL,
            previous_value REAL,
            new_value REAL NOT NULL,
            raw_calculated_value REAL NOT NULL,
            sample_size INTEGER DEFAULT 0,
            confidence_strength REAL DEFAULT 0.0,
            reason_source TEXT DEFAULT 'adaptive_update',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Adaptive config history for auditing
    c.execute("""
        CREATE TABLE IF NOT EXISTS config_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_date DATE NOT NULL,
            config_json TEXT NOT NULL,
            changes_made TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


_init_db()


# ════════════════════════════════════════════════════════════
#   TIME-DECAY WEIGHTING
# ════════════════════════════════════════════════════════════

def _calculate_trade_time_decay(trade_timestamp: str, decay_half_life_days: int = 30) -> float:
    """
    Calculate exponential decay weight for a trade based on age.
    
    Newer trades have weight close to 1.0, older trades decay toward 0.
    Half-life: at decay_half_life_days, weight = 0.5
    
    Formula: weight = 2^(-age_days / half_life_days)
    """
    try:
        trade_date = datetime.fromisoformat(trade_timestamp).date()
        age_days = (datetime.now().date() - trade_date).days
        
        # Exponential decay: weight = 0.5^(age_days / half_life_days)
        weight = 0.5 ** (age_days / decay_half_life_days)
        return max(0.1, min(1.0, weight))  # Clamp between 0.1 and 1.0
    except Exception as e:
        logger.debug(f"Time decay calculation error: {e}")
        return 1.0


def _get_weighted_trade_stats(signal_name: str, decay_half_life_days: int = 30) -> dict:
    """
    Calculate time-decay weighted statistics for a signal.
    
    Older trades influence results less, allowing adaptation to market changes.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT pnl, timestamp FROM trade_records 
            WHERE signal_name = ?
        """, (signal_name,))
        
        trades = c.fetchall()
        conn.close()
        
        if not trades:
            return {"total_weighted_trades": 0, "weighted_wins": 0, "weighted_losses": 0}
        
        total_weight = 0.0
        weighted_wins = 0.0
        weighted_losses = 0.0
        
        for pnl, timestamp in trades:
            weight = _calculate_trade_time_decay(timestamp, decay_half_life_days)
            total_weight += weight
            
            if pnl > 0:
                weighted_wins += weight
            else:
                weighted_losses += weight
        
        return {
            "total_weighted_trades": total_weight,
            "weighted_wins": weighted_wins,
            "weighted_losses": weighted_losses,
        }
    except Exception as e:
        logger.error(f"Failed to calculate weighted stats: {e}")
        return {}


# ════════════════════════════════════════════════════════════
#   SAMPLE-SIZE CONFIDENCE STRENGTH
# ════════════════════════════════════════════════════════════

def _calculate_confidence_strength(total_trades: int, min_trades: int = 10, target_trades: int = 100) -> float:
    """
    Calculate how confident we are in a multiplier based on sample size.
    
    Formula: confidence = min(total_trades / target_trades, 1.0)
    
    - 10 trades:   0.1 (10% influence)
    - 50 trades:   0.5 (50% influence)
    - 100+ trades: 1.0 (100% influence)
    
    This is used to blend old multiplier with new calculated value.
    """
    if total_trades < min_trades:
        return 0.0  # Not enough data, keep old multiplier
    
    confidence = min(total_trades / target_trades, 1.0)
    return round(confidence, 2)


# ════════════════════════════════════════════════════════════
#   EMA SMOOTHING (Exponential Moving Average)
# ════════════════════════════════════════════════════════════

def _apply_ema_smoothing(
    old_multiplier: float,
    new_multiplier: float,
    smoothing_alpha: float = 0.2,
    confidence_strength: float = 1.0,
) -> float:
    """
    Apply exponential moving average to smooth multiplier transitions.
    
    Formula: smoothed = (old * (1 - alpha)) + (new * alpha)
    
    Default alpha = 0.2:
    - 80% old multiplier
    - 20% new calculated value
    
    Confidence strength scales the new multiplier influence:
    - Low confidence (few trades): use more old multiplier
    - High confidence (many trades): use more new multiplier
    """
    # Adjust alpha based on confidence strength
    adjusted_alpha = smoothing_alpha * confidence_strength
    
    smoothed = (old_multiplier * (1 - adjusted_alpha)) + (new_multiplier * adjusted_alpha)
    return round(smoothed, 2)


def _apply_daily_adjustment_limit(
    old_multiplier: float,
    new_multiplier: float,
    max_daily_change_pct: float = 5.0,
) -> float:
    """
    Limit how much a multiplier can change in a single daily update.
    
    Example with max_daily_change_pct = 5%:
    - old_multiplier = 1.0
    - new_multiplier = 0.7 (30% change)
    - allowed_min = 0.95 (5% reduction)
    - applied = 0.95 (limited)
    
    Prevents wild swings from short-term streaks.
    """
    max_change = old_multiplier * (max_daily_change_pct / 100.0)
    
    lower_bound = old_multiplier - max_change
    upper_bound = old_multiplier + max_change
    
    limited = max(lower_bound, min(upper_bound, new_multiplier))
    return round(limited, 2)


# ════════════════════════════════════════════════════════════
#   MULTIPLIER HISTORY MANAGEMENT
# ════════════════════════════════════════════════════════════

def _get_historical_multiplier(multiplier_type: str, multiplier_key: str) -> Optional[float]:
    """
    Get the stored multiplier from previous day (for EMA smoothing).
    
    Returns None if no previous multiplier exists (first update).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT multiplier_value FROM multiplier_history
            WHERE multiplier_type = ? AND multiplier_key = ?
        """, (multiplier_type, multiplier_key))
        
        row = c.fetchone()
        conn.close()
        
        if row:
            return row[0]
        return None
    except Exception as e:
        logger.error(f"Failed to get historical multiplier: {e}")
        return None


def _store_multiplier_history(
    multiplier_type: str,
    multiplier_key: str,
    multiplier_value: float,
    raw_calculated_value: float,
    sample_size: int,
    confidence_strength: float,
):
    """
    Store multiplier for EMA smoothing on next update.
    
    multiplier_type: "signal", "time_window", "symbol_sl", or "market_regime"
    multiplier_key: signal name, time window, or symbol
    multiplier_value: final applied multiplier (after smoothing)
    raw_calculated_value: calculated before smoothing (for logging)
    sample_size: number of trades used
    confidence_strength: 0.0-1.0 confidence in calculation
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("""
            SELECT multiplier_value FROM multiplier_history
            WHERE multiplier_type = ? AND multiplier_key = ?
        """, (multiplier_type, multiplier_key))
        existing = c.fetchone()
        previous_value = existing[0] if existing else None
        
        c.execute("""
            INSERT INTO multiplier_history 
            (multiplier_type, multiplier_key, multiplier_value, raw_calculated_value, 
             sample_size, confidence_strength)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(multiplier_type, multiplier_key) DO UPDATE SET
                multiplier_value = ?,
                raw_calculated_value = ?,
                sample_size = ?,
                confidence_strength = ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            multiplier_type, multiplier_key, multiplier_value, raw_calculated_value,
            sample_size, confidence_strength,
            multiplier_value, raw_calculated_value, sample_size, confidence_strength
        ))

        c.execute("""
            INSERT INTO multiplier_change_log
            (multiplier_type, multiplier_key, previous_value, new_value, raw_calculated_value,
             sample_size, confidence_strength, reason_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            multiplier_type,
            multiplier_key,
            previous_value,
            multiplier_value,
            raw_calculated_value,
            sample_size,
            confidence_strength,
            "adaptive_update",
        ))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Failed to store multiplier history: {e}")


def _save_config_history(config_dict: dict, changes_made: str):
    """Save adaptive config update to history table for auditing."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        config_json = json.dumps(config_dict)
        today = datetime.now().date()
        
        c.execute("""
            INSERT INTO config_history (config_date, config_json, changes_made)
            VALUES (?, ?, ?)
        """, (today, config_json, changes_made))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Failed to save config history: {e}")


# ════════════════════════════════════════════════════════════
#   RECORD TRADES
# ════════════════════════════════════════════════════════════

def record_trade(
    signal_name: str,
    symbol: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    confidence: float,
    time_window: str,
    drawdown: float = 0.0,
    sl_hit: bool = False,
    recovered: bool = False,
) -> bool:
    """
    Record a completed trade.
    Automatically updates signal, time-window, and symbol statistics.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            INSERT INTO trade_records 
            (signal_name, symbol, entry_price, exit_price, pnl, confidence, 
             time_window, drawdown, sl_hit, recovered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_name, symbol, entry_price, exit_price, pnl, confidence,
            time_window, drawdown, int(sl_hit), int(recovered)
        ))
        
        conn.commit()
        conn.close()
        
        # Update derived statistics
        update_signal_stats(signal_name)
        update_time_window_stats(time_window)
        update_symbol_stats(symbol)
        
        logger.debug(
            f"📊 Trade recorded: {signal_name} | {symbol} | PnL={pnl:.2f} | "
            f"Window={time_window} | DD={drawdown:.1f}%"
        )
        return True
        
    except Exception as e:
        logger.error(f"Failed to record trade: {e}")
        return False


# ════════════════════════════════════════════════════════════
#   UPDATE SIGNAL STATISTICS
# ════════════════════════════════════════════════════════════

def update_signal_stats(signal_name: str):
    """Recalculate signal statistics from trade records."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Fetch all trades for this signal
        c.execute("""
            SELECT pnl, drawdown FROM trade_records 
            WHERE signal_name = ?
        """, (signal_name,))
        
        trades = c.fetchall()
        if not trades:
            conn.close()
            return
        
        total_trades = len(trades)
        winning_trades = sum(1 for pnl, _ in trades if pnl > 0)
        losing_trades = total_trades - winning_trades
        
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        winning_pnls = [pnl for pnl, _ in trades if pnl >= 0]
        losing_pnls = [abs(pnl) for pnl, _ in trades if pnl < 0]
        
        avg_profit = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
        avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
        
        avg_rr = avg_profit / avg_loss if avg_loss > 0 else 0.0
        avg_drawdown = sum(dd for _, dd in trades) / total_trades if total_trades > 0 else 0.0
        
        # Update or insert
        c.execute("""
            INSERT INTO signal_performance 
            (signal_name, total_trades, winning_trades, losing_trades, 
             win_rate, avg_profit, avg_loss, avg_rr, avg_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_name) DO UPDATE SET
                total_trades = ?,
                winning_trades = ?,
                losing_trades = ?,
                win_rate = ?,
                avg_profit = ?,
                avg_loss = ?,
                avg_rr = ?,
                avg_drawdown = ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            signal_name, total_trades, winning_trades, losing_trades,
            win_rate, avg_profit, avg_loss, avg_rr, avg_drawdown,
            total_trades, winning_trades, losing_trades,
            win_rate, avg_profit, avg_loss, avg_rr, avg_drawdown
        ))
        
        conn.commit()
        conn.close()
        
        logger.debug(
            f"🔧 Signal stats updated: {signal_name} | "
            f"{winning_trades}/{total_trades} wins | "
            f"WR={win_rate:.1f}% | RR={avg_rr:.2f}"
        )
        
    except Exception as e:
        logger.error(f"Failed to update signal stats: {e}")


# ════════════════════════════════════════════════════════════
#   UPDATE TIME WINDOW STATISTICS
# ════════════════════════════════════════════════════════════

def update_time_window_stats(time_window: str):
    """Recalculate time window statistics from trade records."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT pnl FROM trade_records 
            WHERE time_window = ?
        """, (time_window,))
        
        trades = c.fetchall()
        if not trades:
            conn.close()
            return
        
        trade_count = len(trades)
        pnls = [pnl for (pnl,) in trades]
        
        winning_trades = sum(1 for pnl in pnls if pnl > 0)
        win_rate = (winning_trades / trade_count * 100) if trade_count > 0 else 0.0
        avg_pnl = sum(pnls) / trade_count if trade_count > 0 else 0.0
        failure_rate = ((trade_count - winning_trades) / trade_count * 100) if trade_count > 0 else 0.0
        
        # Update or insert
        c.execute("""
            INSERT INTO time_window_performance 
            (time_window, trade_count, win_rate, avg_pnl, failure_rate)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(time_window) DO UPDATE SET
                trade_count = ?,
                win_rate = ?,
                avg_pnl = ?,
                failure_rate = ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            time_window, trade_count, win_rate, avg_pnl, failure_rate,
            trade_count, win_rate, avg_pnl, failure_rate
        ))
        
        conn.commit()
        conn.close()
        
        logger.debug(
            f"⏰ Time window stats: {time_window} | "
            f"{trade_count} trades | WR={win_rate:.1f}% | Avg PnL={avg_pnl:.2f}"
        )
        
    except Exception as e:
        logger.error(f"Failed to update time window stats: {e}")


# ════════════════════════════════════════════════════════════
#   UPDATE SYMBOL STATISTICS
# ════════════════════════════════════════════════════════════

def update_symbol_stats(symbol: str):
    """Recalculate symbol behavior from trade records."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT sl_hit, recovered, drawdown FROM trade_records 
            WHERE symbol = ?
        """, (symbol,))
        
        trades = c.fetchall()
        if not trades:
            conn.close()
            return
        
        total_trades = len(trades)
        sl_hit_count = sum(1 for sl_hit, _, _ in trades if sl_hit)
        recovered_count = sum(1 for _, recovered, _ in trades if recovered)
        
        sl_hit_freq = (sl_hit_count / total_trades * 100) if total_trades > 0 else 0.0
        recovery_prob = (recovered_count / sl_hit_count * 100) if sl_hit_count > 0 else 0.0
        
        drawdowns = [dd for _, _, dd in trades]
        avg_drawdown = sum(drawdowns) / len(drawdowns) if drawdowns else 0.0
        
        # Volatility profile based on drawdown
        if avg_drawdown > 3.0:
            volatility_profile = "HIGH"
        elif avg_drawdown > 1.5:
            volatility_profile = "NORMAL"
        else:
            volatility_profile = "LOW"
        
        # Update or insert
        c.execute("""
            INSERT INTO symbol_behavior 
            (symbol, sl_hit_freq, recovery_prob, avg_drawdown, volatility_profile)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                sl_hit_freq = ?,
                recovery_prob = ?,
                avg_drawdown = ?,
                volatility_profile = ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            symbol, sl_hit_freq, recovery_prob, avg_drawdown, volatility_profile,
            sl_hit_freq, recovery_prob, avg_drawdown, volatility_profile
        ))
        
        conn.commit()
        conn.close()
        
        logger.debug(
            f"📈 Symbol stats: {symbol} | "
            f"SL Hit={sl_hit_freq:.1f}% | Recovery={recovery_prob:.1f}% | "
            f"Volatility={volatility_profile}"
        )
        
    except Exception as e:
        logger.error(f"Failed to update symbol stats: {e}")


# ════════════════════════════════════════════════════════════
#   GETTERS FOR ADAPTIVE CONFIG
# ════════════════════════════════════════════════════════════

def get_signal_stats(signal_name: str) -> dict | None:
    """Get performance stats for a specific signal."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT total_trades, winning_trades, losing_trades, win_rate,
                   avg_profit, avg_loss, avg_rr, avg_drawdown
            FROM signal_performance
            WHERE signal_name = ?
        """, (signal_name,))
        
        row = c.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "signal_name": signal_name,
            "total_trades": row[0],
            "winning_trades": row[1],
            "losing_trades": row[2],
            "win_rate": row[3],
            "avg_profit": row[4],
            "avg_loss": row[5],
            "avg_rr": row[6],
            "avg_drawdown": row[7],
        }
    except Exception as e:
        logger.error(f"Failed to get signal stats: {e}")
        return None


def get_all_signal_stats() -> list[dict]:
    """Get all signal statistics."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT signal_name, total_trades, winning_trades, losing_trades, 
                   win_rate, avg_profit, avg_loss, avg_rr, avg_drawdown
            FROM signal_performance
            ORDER BY total_trades DESC
        """)
        
        rows = c.fetchall()
        conn.close()
        
        return [
            {
                "signal_name": row[0],
                "total_trades": row[1],
                "winning_trades": row[2],
                "losing_trades": row[3],
                "win_rate": row[4],
                "avg_profit": row[5],
                "avg_loss": row[6],
                "avg_rr": row[7],
                "avg_drawdown": row[8],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to get all signal stats: {e}")
        return []


def _stats_from_pnls(pnls: list[float]) -> dict:
    total = len(pnls)
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = total - wins
    win_rate = (wins / total * 100) if total else 0.0
    return {
        "total_trades": total,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(win_rate, 2),
    }


def _trade_record_pnls(signal_name: str, since: datetime | None = None) -> list[float]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if since is None:
        c.execute(
            "SELECT pnl FROM trade_records WHERE signal_name = ? ORDER BY timestamp DESC",
            (signal_name,),
        )
    else:
        c.execute(
            "SELECT pnl FROM trade_records WHERE signal_name = ? AND timestamp >= ? ORDER BY timestamp DESC",
            (signal_name, since.strftime("%Y-%m-%d %H:%M:%S")),
        )
    rows = c.fetchall()
    conn.close()
    return [float(row[0] or 0.0) for row in rows]


def get_signal_execution_policy(
    signal_name: str,
    min_trades: int = 10,
    min_win_rate: float = 40.0,
    rolling_days: int = 20,
) -> dict:
    """
    First-class execution policy for strategy-set admission.
    Uses current-session data first, then a recent rolling window. Lifetime
    stats are reported for context but do not hard-block fresh sessions.
    """
    signal_name = str(signal_name or "").strip()
    if not signal_name:
        return {
            "signal_name": signal_name,
            "suppressed": False,
            "reason": "No signal name",
            "scope": "none",
            "sample_size": 0,
            "win_rate": 0.0,
        }

    try:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rolling_start = datetime.now() - timedelta(days=max(1, int(rolling_days)))

        today_stats = _stats_from_pnls(_trade_record_pnls(signal_name, today_start))
        rolling_stats = _stats_from_pnls(_trade_record_pnls(signal_name, rolling_start))
        lifetime = get_signal_stats(signal_name) or {}

        selected_scope = "insufficient_recent_sample"
        selected = today_stats
        if today_stats["total_trades"] >= min_trades:
            selected_scope = "today"
            selected = today_stats
        elif rolling_stats["total_trades"] >= min_trades:
            selected_scope = f"rolling_{rolling_days}d"
            selected = rolling_stats
        else:
            selected = rolling_stats

        suppressed = (
            selected_scope != "insufficient_recent_sample"
            and selected["total_trades"] >= min_trades
            and selected["win_rate"] < min_win_rate
        )

        if suppressed:
            reason = (
                f"{signal_name} suppressed: {selected_scope} win rate "
                f"{selected['win_rate']:.1f}% from {selected['total_trades']} trades "
                f"is below {min_win_rate:.1f}%"
            )
        elif selected_scope == "insufficient_recent_sample":
            reason = (
                f"{signal_name} not execution-suppressed: only "
                f"{selected['total_trades']} recent trades; need {min_trades}"
            )
        else:
            reason = (
                f"{signal_name} allowed: {selected_scope} win rate "
                f"{selected['win_rate']:.1f}% from {selected['total_trades']} trades"
            )

        return {
            "signal_name": signal_name,
            "suppressed": suppressed,
            "reason": reason,
            "scope": selected_scope,
            "sample_size": selected["total_trades"],
            "win_rate": selected["win_rate"],
            "min_trades": min_trades,
            "min_win_rate": min_win_rate,
            "today": today_stats,
            "rolling": rolling_stats,
            "lifetime": lifetime,
        }
    except Exception as e:
        logger.error(f"Failed to get signal execution policy: {e}")
        return {
            "signal_name": signal_name,
            "suppressed": False,
            "reason": f"Policy unavailable: {e}",
            "scope": "error",
            "sample_size": 0,
            "win_rate": 0.0,
        }


def get_time_window_stats(time_window: str) -> dict | None:
    """Get performance stats for a specific time window."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT trade_count, win_rate, avg_pnl, failure_rate
            FROM time_window_performance
            WHERE time_window = ?
        """, (time_window,))
        
        row = c.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "time_window": time_window,
            "trade_count": row[0],
            "win_rate": row[1],
            "avg_pnl": row[2],
            "failure_rate": row[3],
        }
    except Exception as e:
        logger.error(f"Failed to get time window stats: {e}")
        return None


def get_all_time_window_stats() -> list[dict]:
    """Get all time window statistics."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT time_window, trade_count, win_rate, avg_pnl, failure_rate
            FROM time_window_performance
            ORDER BY time_window
        """)
        
        rows = c.fetchall()
        conn.close()
        
        return [
            {
                "time_window": row[0],
                "trade_count": row[1],
                "win_rate": row[2],
                "avg_pnl": row[3],
                "failure_rate": row[4],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to get all time window stats: {e}")
        return []


def get_symbol_stats(symbol: str) -> dict | None:
    """Get behavior stats for a specific symbol."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT sl_hit_freq, recovery_prob, avg_drawdown, volatility_profile
            FROM symbol_behavior
            WHERE symbol = ?
        """, (symbol,))
        
        row = c.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "symbol": symbol,
            "sl_hit_freq": row[0],
            "recovery_prob": row[1],
            "avg_drawdown": row[2],
            "volatility_profile": row[3],
        }
    except Exception as e:
        logger.error(f"Failed to get symbol stats: {e}")
        return None


def get_all_symbol_stats() -> list[dict]:
    """Get all symbol statistics."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("""
            SELECT symbol, sl_hit_freq, recovery_prob, avg_drawdown, volatility_profile
            FROM symbol_behavior
            ORDER BY symbol
        """)
        
        rows = c.fetchall()
        conn.close()
        
        return [
            {
                "symbol": row[0],
                "sl_hit_freq": row[1],
                "recovery_prob": row[2],
                "avg_drawdown": row[3],
                "volatility_profile": row[4],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to get all symbol stats: {e}")
        return []


# ════════════════════════════════════════════════════════════
#   CONFIDENCE MULTIPLIER CALCULATION
# ════════════════════════════════════════════════════════════

def get_confidence_multiplier(signal_name: str, min_trades: int = 10) -> float:
    """
    Calculate confidence multiplier for a signal based on win rate.
    
    Multiplier range: 0.4 to 1.2
    - Below 40% win rate: 0.4 (suppress)
    - 40-50% win rate: 0.6-0.8 (reduce)
    - 50-60% win rate: 0.9-1.0 (neutral)
    - 60-70% win rate: 1.0-1.1 (boost)
    - Above 70% win rate: 1.2 (strong boost)
    
    Requires minimum N trades for reliability.
    """
    stats = get_signal_stats(signal_name)
    
    # Not enough data — neutral multiplier
    if not stats or stats["total_trades"] < min_trades:
        return 1.0
    
    wr = stats["win_rate"]
    
    if wr < 40:
        return 0.4
    elif wr < 50:
        return 0.6 + (wr - 40) / 10 * 0.2
    elif wr < 60:
        return 0.8 + (wr - 50) / 10 * 0.1
    elif wr < 70:
        return 0.9 + (wr - 60) / 10 * 0.2
    else:
        return 1.2


def get_time_window_multiplier(time_window: str, min_trades: int = 20) -> float:
    """
    Calculate multiplier for a time window based on win rate and average PnL.
    
    Multiplier range: 0.4 to 1.2
    """
    stats = get_time_window_stats(time_window)
    
    if not stats or stats["trade_count"] < min_trades:
        return 1.0
    
    wr = stats["win_rate"]
    
    if wr < 40:
        return 0.4
    elif wr < 50:
        return 0.6 + (wr - 40) / 10 * 0.2
    elif wr < 60:
        return 0.8 + (wr - 50) / 10 * 0.1
    elif wr < 70:
        return 0.9 + (wr - 60) / 10 * 0.2
    else:
        return 1.2


def get_symbol_sl_adjustment(symbol: str) -> float:
    """
    Adjust stop loss percentage based on symbol volatility and recovery probability.
    
    Returns percentage adjustment.
    Example: 0.8 means use 80% of base SL (tighter)
    """
    stats = get_symbol_stats(symbol)
    
    if not stats:
        return 1.0
    
    volatility = stats["volatility_profile"]
    
    if volatility == "HIGH":
        return 1.2  # Wider SL for volatile stocks
    elif volatility == "LOW":
        return 0.8  # Tighter SL for stable stocks
    else:
        return 1.0  # Normal


# ════════════════════════════════════════════════════════════
#   JSON SNAPSHOTS FOR BACKUP & ANALYSIS
# ════════════════════════════════════════════════════════════

def save_reflection_snapshot():
    """Save current statistics to JSON for backup and manual review."""
    try:
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "signals": get_all_signal_stats(),
            "time_windows": get_all_time_window_stats(),
            "symbols": get_all_symbol_stats(),
        }
        
        filename = SNAPSHOTS_DIR / f"reflection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(filename, "w") as f:
            json.dump(snapshot, f, indent=2)
        
        logger.info(f"💾 Reflection snapshot saved: {filename}")
        return str(filename)
        
    except Exception as e:
        logger.error(f"Failed to save reflection snapshot: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   RESET ADAPTIVE LEARNING DATA
# ════════════════════════════════════════════════════════════

def reset_adaptive_learning_data(keep_trade_records: bool = True):
    """
    Safely clear fake/demo adaptive learning data while preserving system integrity.
    
    IMPORTANT: This ONLY deletes adaptive learning statistics, NOT trade history or configs.
    
    Args:
        keep_trade_records: If True, preserve trade_records for future retraining
    
    Clears:
        - signal_performance (all entries)
        - time_window_performance (all entries)
        - symbol_behavior (all entries)
        - multiplier_history (all entries)
        - multiplier_change_log (all entries)
        - config_history (all entries)
        - trade_records (optional, based on parameter)
    
    Preserves:
        - Database schema (tables and indexes)
        - System integrity
        - All other data
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        logger.warning("🗑️ RESETTING ADAPTIVE LEARNING DATA...")
        
        # Delete demo/test adaptive statistics
        c.execute("DELETE FROM signal_performance")
        signals_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {signals_deleted} signal performance entries")
        
        c.execute("DELETE FROM time_window_performance")
        windows_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {windows_deleted} time window entries")
        
        c.execute("DELETE FROM symbol_behavior")
        symbols_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {symbols_deleted} symbol behavior entries")
        
        c.execute("DELETE FROM multiplier_history")
        mult_hist_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {mult_hist_deleted} multiplier history entries")
        
        c.execute("DELETE FROM multiplier_change_log")
        change_log_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {change_log_deleted} change log entries")
        
        c.execute("DELETE FROM config_history")
        config_hist_deleted = c.rowcount
        logger.info(f"  ✓ Deleted {config_hist_deleted} config history entries")
        
        if keep_trade_records:
            logger.info(f"  ✓ Preserved trade_records for future retraining")
        else:
            c.execute("DELETE FROM trade_records")
            trades_deleted = c.rowcount
            logger.info(f"  ✓ Deleted {trades_deleted} trade records")
        
        conn.commit()
        conn.close()
        
        logger.warning("✅ Adaptive learning data reset complete. System ready for production learning.")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to reset adaptive learning data: {e}")
        return False


# ════════════════════════════════════════════════════════════
#   DATABASE STATS
# ════════════════════════════════════════════════════════════

def get_database_summary() -> dict:
    """Get high-level summary of all statistics."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM trade_records")
        total_trades = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM signal_performance")
        unique_signals = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM time_window_performance")
        unique_windows = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM symbol_behavior")
        unique_symbols = c.fetchone()[0]
        
        conn.close()
        
        return {
            "total_trades_recorded": total_trades,
            "unique_signals": unique_signals,
            "unique_time_windows": unique_windows,
            "unique_symbols": unique_symbols,
            "last_updated": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to get database summary: {e}")
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print(get_database_summary())
