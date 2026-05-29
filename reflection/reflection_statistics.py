# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/reflection_statistics.py
#
#   Reflection Statistics Layer
#   Tracks signal performance, market conditions, adaptive parameters
#
#   Core responsibilities:
#   - Track signal win rates
#   - Analyze time-window performance
#   - Measure per-symbol behavior
#   - Generate adaptive configuration values
#   - Provide reliability estimates
#
#   Does NOT:
#   - Place trades
#   - Create trading signals
#   - Create debate systems
# ============================================================

import sqlite3
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = "data/reflection_statistics.db"


# ════════════════════════════════════════════════════════════
#   DATABASE SCHEMA INITIALIZATION
# ════════════════════════════════════════════════════════════

def initialize_database():
    """Create reflection statistics database schema."""
    Path("data").mkdir(exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Signal performance tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_name TEXT NOT NULL,
            trade_date DATE NOT NULL,
            entry_price REAL,
            exit_price REAL,
            profit_loss REAL,
            win INTEGER,
            drawdown REAL,
            recovery_time_minutes INTEGER,
            confidence_at_trade REAL,
            strategy_context TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(signal_name, trade_date, entry_price)
        )
    """)
    
    # Time window performance
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS time_window_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_window TEXT NOT NULL,
            trade_date DATE NOT NULL,
            total_trades INTEGER,
            winning_trades INTEGER,
            losing_trades INTEGER,
            total_profit_loss REAL,
            win_rate REAL,
            avg_rr REAL,
            fake_breakout_count INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(time_window, trade_date)
        )
    """)
    
    # Per-symbol behavior
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS symbol_behavior (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trade_date DATE NOT NULL,
            avg_drawdown REAL,
            max_drawdown REAL,
            volatility_pct REAL,
            sl_hit_count INTEGER,
            recovery_rate REAL,
            recovery_avg_time_minutes INTEGER,
            confidence_multiplier REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, trade_date)
        )
    """)
    
    # Market observations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_time TIMESTAMP NOT NULL,
            market_condition TEXT,
            trend_strength TEXT,
            volatility_regime TEXT,
            breakout_frequency TEXT,
            reversal_frequency TEXT,
            fake_signal_count INTEGER,
            observations_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Adaptive configuration history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adaptive_config_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_date DATE NOT NULL,
            config_json TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(config_date)
        )
    """)
    
    # Indexes for performance
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_name ON signal_performance(signal_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_date ON signal_performance(trade_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON symbol_behavior(symbol)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_time_window ON time_window_performance(time_window)")
    
    conn.commit()
    conn.close()
    logger.info("✅ Reflection statistics database initialized")


# ════════════════════════════════════════════════════════════
#   SIGNAL PERFORMANCE TRACKING
# ════════════════════════════════════════════════════════════

def record_signal_trade(
    signal_name: str,
    entry_price: float,
    exit_price: float,
    profit_loss: float,
    win: bool,
    drawdown: float,
    confidence: float = 0.5,
    strategy_context: str = "",
    recovery_time_minutes: Optional[int] = None,
):
    """Record a completed trade for signal performance tracking."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO signal_performance
            (signal_name, trade_date, entry_price, exit_price, profit_loss, 
             win, drawdown, confidence_at_trade, strategy_context, recovery_time_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_name,
            date.today(),
            entry_price,
            exit_price,
            profit_loss,
            1 if win else 0,
            drawdown,
            confidence,
            strategy_context,
            recovery_time_minutes,
        ))
        
        conn.commit()
        conn.close()
        logger.debug(f"📊 Signal recorded: {signal_name} | W={win} | PL={profit_loss}")
    except Exception as e:
        logger.error(f"Error recording signal trade: {e}")


def get_signal_win_rates(start_date: Optional[date] = None, end_date: Optional[date] = None) -> Dict[str, Dict]:
    """Get aggregated win rates for all signals in date range."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT
                signal_name,
                COUNT(*) as total_trades,
                SUM(win) as wins,
                AVG(CAST(win AS FLOAT)) as win_rate,
                AVG(profit_loss) as avg_profit_loss,
                AVG(drawdown) as avg_drawdown,
                MAX(drawdown) as max_drawdown,
                AVG(confidence_at_trade) as avg_confidence
            FROM signal_performance
            WHERE 1=1
        """
        params = []
        
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        
        query += " GROUP BY signal_name ORDER BY win_rate DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        result = {}
        for row in rows:
            signal_name, total, wins, wr, avg_pl, avg_dd, max_dd, avg_conf = row
            result[signal_name] = {
                "total_trades": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": round(wr * 100, 2),
                "avg_profit_loss": round(avg_pl, 2),
                "avg_drawdown": round(avg_dd, 2),
                "max_drawdown": round(max_dd, 2),
                "avg_confidence": round(avg_conf, 2),
            }
        
        return result
    except Exception as e:
        logger.error(f"Error getting signal win rates: {e}")
        return {}


# ════════════════════════════════════════════════════════════
#   TIME WINDOW PERFORMANCE
# ════════════════════════════════════════════════════════════

def record_time_window_performance(
    time_window: str,  # e.g. "9:15-10:00"
    total_trades: int,
    winning_trades: int,
    total_profit_loss: float,
    fake_breakout_count: int = 0,
):
    """Record performance for a specific time window."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        losing_trades = total_trades - winning_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        cursor.execute("""
            INSERT OR REPLACE INTO time_window_performance
            (time_window, trade_date, total_trades, winning_trades, losing_trades,
             total_profit_loss, win_rate, fake_breakout_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time_window,
            date.today(),
            total_trades,
            winning_trades,
            losing_trades,
            total_profit_loss,
            win_rate,
            fake_breakout_count,
        ))
        
        conn.commit()
        conn.close()
        logger.debug(f"⏰ Time window {time_window}: {winning_trades}/{total_trades}")
    except Exception as e:
        logger.error(f"Error recording time window performance: {e}")


def get_time_window_analysis(lookback_days: int = 30) -> Dict[str, Dict]:
    """Get aggregated time window performance."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT
                time_window,
                COUNT(*) as sample_count,
                AVG(win_rate) as avg_win_rate,
                AVG(total_profit_loss) as avg_profit_per_day,
                AVG(fake_breakout_count) as avg_fakeouts
            FROM time_window_performance
            WHERE trade_date >= date('now', '-' || ? || ' days')
            GROUP BY time_window
            ORDER BY avg_win_rate DESC
        """, (lookback_days,))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = {}
        for row in rows:
            time_window, samples, wr, avg_pl, fakeouts = row
            result[time_window] = {
                "samples": samples,
                "win_rate": round(wr * 100, 2),
                "avg_daily_profit": round(avg_pl, 2),
                "avg_fakeouts": round(fakeouts, 1),
            }
        
        return result
    except Exception as e:
        logger.error(f"Error getting time window analysis: {e}")
        return {}


# ════════════════════════════════════════════════════════════
#   PER-SYMBOL BEHAVIOR TRACKING
# ════════════════════════════════════════════════════════════

def record_symbol_behavior(
    symbol: str,
    avg_drawdown: float,
    max_drawdown: float,
    volatility_pct: float,
    sl_hit_count: int,
    recovery_rate: float,
    recovery_avg_time_minutes: Optional[int] = None,
):
    """Record behavioral metrics for a specific symbol."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO symbol_behavior
            (symbol, trade_date, avg_drawdown, max_drawdown, volatility_pct,
             sl_hit_count, recovery_rate, recovery_avg_time_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            date.today(),
            avg_drawdown,
            max_drawdown,
            volatility_pct,
            sl_hit_count,
            recovery_rate,
            recovery_avg_time_minutes,
        ))
        
        conn.commit()
        conn.close()
        logger.debug(f"📈 Symbol behavior: {symbol} | DD={avg_drawdown}% | Recov={recovery_rate}%")
    except Exception as e:
        logger.error(f"Error recording symbol behavior: {e}")


def get_symbol_analysis(lookback_days: int = 60) -> Dict[str, Dict]:
    """Get aggregated per-symbol behavior."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT
                symbol,
                COUNT(*) as trade_count,
                AVG(avg_drawdown) as avg_drawdown,
                MAX(max_drawdown) as observed_max_drawdown,
                AVG(volatility_pct) as volatility,
                AVG(recovery_rate) as recovery_rate,
                AVG(recovery_avg_time_minutes) as avg_recovery_minutes,
                ROUND(AVG(avg_drawdown) * 1.2, 2) as recommended_sl
            FROM symbol_behavior
            WHERE trade_date >= date('now', '-' || ? || ' days')
            GROUP BY symbol
            ORDER BY recovery_rate DESC
        """, (lookback_days,))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = {}
        for row in rows:
            (symbol, count, avg_dd, max_dd, vol, recovery,
             avg_recovery_mins, rec_sl) = row
            result[symbol] = {
                "trade_count": count,
                "avg_drawdown": round(avg_dd, 2),
                "max_drawdown": round(max_dd, 2),
                "volatility": round(vol, 2),
                "recovery_rate": round(recovery, 2),
                "avg_recovery_minutes": avg_recovery_mins,
                "recommended_sl": rec_sl,
            }
        
        return result
    except Exception as e:
        logger.error(f"Error getting symbol analysis: {e}")
        return {}


# ════════════════════════════════════════════════════════════
#   MARKET OBSERVATIONS
# ════════════════════════════════════════════════════════════

def record_market_observation(
    market_condition: str,
    trend_strength: str,
    volatility_regime: str,
    breakout_frequency: str,
    reversal_frequency: str,
    fake_signal_count: int = 0,
    observations_json: Optional[Dict] = None,
):
    """Record market condition observation."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        obs_str = json.dumps(observations_json) if observations_json else "{}"
        
        cursor.execute("""
            INSERT INTO market_observations
            (observation_time, market_condition, trend_strength, volatility_regime,
             breakout_frequency, reversal_frequency, fake_signal_count, observations_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(),
            market_condition,
            trend_strength,
            volatility_regime,
            breakout_frequency,
            reversal_frequency,
            fake_signal_count,
            obs_str,
        ))
        
        conn.commit()
        conn.close()
        logger.debug(f"🔍 Market obs: {market_condition} | Trend={trend_strength} | Vol={volatility_regime}")
    except Exception as e:
        logger.error(f"Error recording market observation: {e}")


def get_latest_market_observation() -> Optional[Dict]:
    """Get most recent market observation."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT market_condition, trend_strength, volatility_regime,
                   breakout_frequency, reversal_frequency, fake_signal_count,
                   observations_json, observation_time
            FROM market_observations
            ORDER BY observation_time DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        (market_cond, trend, vol, breakout, reversal, fakes,
         obs_json, obs_time) = row
        
        try:
            obs_data = json.loads(obs_json) if obs_json else {}
        except:
            obs_data = {}
        
        return {
            "market_condition": market_cond,
            "trend_strength": trend,
            "volatility_regime": vol,
            "breakout_frequency": breakout,
            "reversal_frequency": reversal,
            "fake_signal_count": fakes,
            "observation_time": obs_time,
            "details": obs_data,
        }
    except Exception as e:
        logger.error(f"Error getting latest market observation: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   ADAPTIVE CONFIGURATION HISTORY
# ════════════════════════════════════════════════════════════

def record_adaptive_config_history(
    config_json: str,
    reason: str = "Adaptive update",
):
    """Record adaptive configuration update in history."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO adaptive_config_history
            (config_date, config_json, reason)
            VALUES (?, ?, ?)
        """, (
            date.today(),
            config_json,
            reason,
        ))
        
        conn.commit()
        conn.close()
        logger.debug(f"📋 Adaptive config recorded: {reason}")
    except Exception as e:
        logger.error(f"Error recording adaptive config: {e}")


# ════════════════════════════════════════════════════════════
#   INITIALIZATION
# ════════════════════════════════════════════════════════════

# Initialize on import
initialize_database()
