# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/cognition_engine.py — Cognitive Observation Loop
#
#   Serial LLM agent system running every 15 minutes.
#   Observes market patterns, generates hypotheses, critiques predictions.
#
#   IMPORTANT: This is RESEARCH ONLY — agents do NOT place trades.
# ============================================================

import json
import logging
import os
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

COGNITION_DIR = Path(__file__).resolve().parent.parent / "data" / "cognition"
COGNITION_DIR.mkdir(parents=True, exist_ok=True)

# P3-9: anchor to project root so engine & dashboard resolve the SAME db file
# regardless of the process working directory.
DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "alcosoft.db")

# ════════════════════════════════════════════════════════════
#   COGNITION DATA MODELS
# ════════════════════════════════════════════════════════════

class CognitionCycle:
    """Represents a single agent observation cycle."""
    def __init__(self, timestamp: str, agent: str, cycle_num: int):
        self.timestamp = timestamp
        self.agent = agent
        self.cycle_num = cycle_num
        self.market_observation = ""
        self.predictions = []
        self.previous_prediction_review = []
        self.regime_notes = ""
        self.anomalies = []
        self.potential_patterns = []
        self.questions_for_future_agents = []
        self.confidence_level = 0.0

    def to_json(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "agent": self.agent,
            "cycle_num": self.cycle_num,
            "market_observation": self.market_observation,
            "predictions": self.predictions,
            "previous_prediction_review": self.previous_prediction_review,
            "regime_notes": self.regime_notes,
            "anomalies": self.anomalies,
            "potential_patterns": self.potential_patterns,
            "questions_for_future_agents": self.questions_for_future_agents,
            "confidence_level": self.confidence_level,
        }

    @staticmethod
    def from_json(data: dict) -> "CognitionCycle":
        obj = CognitionCycle(
            data["timestamp"],
            data["agent"],
            data["cycle_num"]
        )
        obj.market_observation = data.get("market_observation", "")
        obj.predictions = data.get("predictions", [])
        obj.previous_prediction_review = data.get("previous_prediction_review", [])
        obj.regime_notes = data.get("regime_notes", "")
        obj.anomalies = data.get("anomalies", [])
        obj.potential_patterns = data.get("potential_patterns", [])
        obj.questions_for_future_agents = data.get("questions_for_future_agents", [])
        obj.confidence_level = data.get("confidence_level", 0.0)
        return obj


# ════════════════════════════════════════════════════════════
#   COGNITION STORAGE
# ════════════════════════════════════════════════════════════

def _init_cognition_db():
    """Initialize cognition tables if they don't exist."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        # Cognition cycles — each agent observation
        c.execute("""
            CREATE TABLE IF NOT EXISTS cognition_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                agent TEXT NOT NULL,
                cycle_num INTEGER NOT NULL,
                data JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Hypotheses — predictions and assumptions
        c.execute("""
            CREATE TABLE IF NOT EXISTS cognition_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                confidence REAL,
                status TEXT DEFAULT 'active',
                resolution TEXT,
                resolved_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Reviews — outcome tracking
        c.execute("""
            CREATE TABLE IF NOT EXISTS cognition_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                prediction_id TEXT,
                result TEXT,
                analysis TEXT,
                agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Daily reflections — end-of-day cognition summary
        c.execute("""
            CREATE TABLE IF NOT EXISTS cognition_daily_reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                cognition_summary TEXT,
                strongest_patterns TEXT,
                failed_assumptions TEXT,
                regime_behavior TEXT,
                unexpected_anomalies TEXT,
                next_day_watch_themes TEXT,
                unresolved_questions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        conn.close()
        logger.debug("Cognition database initialized")

    except Exception as e:
        logger.error(f"Failed to initialize cognition database: {e}")


def save_cognition_cycle(cycle: CognitionCycle):
    """Save a single agent observation cycle."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        today = date.today().isoformat()
        c.execute("""
            INSERT INTO cognition_cycles (timestamp, date, agent, cycle_num, data)
            VALUES (?, ?, ?, ?, ?)
        """, (
            cycle.timestamp,
            today,
            cycle.agent,
            cycle.cycle_num,
            json.dumps(cycle.to_json())
        ))

        conn.commit()
        conn.close()

        logger.debug(f"Saved cognition cycle: {cycle.agent} #{cycle.cycle_num}")

    except Exception as e:
        logger.error(f"Failed to save cognition cycle: {e}")


def load_today_cognition_cycles() -> list[CognitionCycle]:
    """Load all cognition cycles from today."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        today = date.today().isoformat()
        rows = c.execute("""
            SELECT data FROM cognition_cycles
            WHERE date = ?
            ORDER BY timestamp ASC
        """, (today,)).fetchall()

        conn.close()

        cycles = [CognitionCycle.from_json(json.loads(r[0])) for r in rows]
        return cycles

    except Exception as e:
        logger.warning(f"Failed to load today's cognition cycles: {e}")
        return []


def load_recent_cognition_cycles(limit: int = 20) -> list[CognitionCycle]:
    """Load recent cognition cycles (default last 20)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        rows = c.execute("""
            SELECT data FROM cognition_cycles
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()

        conn.close()

        cycles = [CognitionCycle.from_json(json.loads(r[0])) for r in rows]
        return list(reversed(cycles))  # Return in chronological order

    except Exception as e:
        logger.warning(f"Failed to load recent cognition cycles: {e}")
        return []


# ════════════════════════════════════════════════════════════
#   MARKET SNAPSHOT BUILDER
# ════════════════════════════════════════════════════════════

def build_market_snapshot() -> dict:
    """
    Build current market state for agent observation.
    This is SUMMARIZED input — NOT raw data dumps.
    """
    try:
        from core.state_manager import get_today_stats
        from reflection.reflection_engine import get_all_signal_stats, get_all_time_window_stats
        from core.kotak_client import get_client

        stats = get_today_stats()
        signal_stats = get_all_signal_stats()
        window_stats = get_all_time_window_stats()

        # P2-6 FIX: derive a REAL NIFTY trend instead of the hardcoded
        # `"BULLISH" if True else ...` placeholder that fed fabricated data to the
        # cognition agents every cycle. Fall back to the regime filter, then UNKNOWN.
        nifty_trend = "UNKNOWN"
        nifty_price = None
        try:
            from core.regime_filter import is_bull_day, is_bear_day
            if is_bull_day():
                nifty_trend = "BULLISH"
            elif is_bear_day():
                nifty_trend = "BEARISH"
            else:
                nifty_trend = "NEUTRAL"
        except Exception as exc:
            logger.debug("NIFTY trend (regime) unavailable: %s", exc)
        try:
            import yfinance as yf
            _idx = yf.Ticker("^NSEI")
            _p = (_idx.info or {}).get("regularMarketPrice") or (_idx.info or {}).get("currentPrice")
            if _p:
                nifty_price = float(_p)
        except Exception as exc:
            logger.debug("NIFTY price unavailable: %s", exc)

        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "nifty_trend": nifty_trend,
            "nifty_price": nifty_price,
            "total_trades_today": stats.get("total_trades", 0),
            "winning_trades": stats.get("winning_trades", 0),
            "losing_trades": stats.get("losing_trades", 0),
            "win_rate": round((stats.get("winning_trades", 0) / max(1, stats.get("total_trades", 0))) * 100, 1),
            "gross_pnl": stats.get("gross_pnl", 0),
            "active_positions": stats.get("open_positions", 0),
            "top_signals": [s["signal_name"] for s in signal_stats[:3]],
            "signal_performance": signal_stats,
            "strong_time_windows": [w for w in window_stats if w.get("win_rate", 0) >= 55],
            "weak_time_windows": [w for w in window_stats if w.get("win_rate", 0) < 50],
        }

        return snapshot

    except Exception as e:
        logger.warning(f"Failed to build market snapshot: {e}")
        return {"timestamp": datetime.now().isoformat(), "error": str(e)}


# ════════════════════════════════════════════════════════════
#   HYPOTHESIS & PREDICTION TRACKING
# ════════════════════════════════════════════════════════════

def save_hypothesis(hypothesis: str, confidence: float, agent: str):
    """Save a hypothesis for future tracking."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        today = date.today().isoformat()
        c.execute("""
            INSERT INTO cognition_hypotheses (timestamp, date, hypothesis, confidence, status)
            VALUES (?, ?, ?, ?, 'active')
        """, (
            datetime.now().isoformat(),
            today,
            hypothesis,
            confidence
        ))

        conn.commit()
        conn.close()

        logger.debug(f"Hypothesis saved by {agent}: {hypothesis[:50]}...")

    except Exception as e:
        logger.error(f"Failed to save hypothesis: {e}")


def get_unresolved_hypotheses() -> list:
    """Get all active hypotheses."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        rows = c.execute("""
            SELECT * FROM cognition_hypotheses
            WHERE status = 'active'
            ORDER BY confidence DESC
        """).fetchall()

        conn.close()
        return [dict(r) for r in rows]

    except Exception as e:
        logger.warning(f"Failed to load hypotheses: {e}")
        return []


def save_prediction_review(prediction_id: str, result: str, analysis: str, agent: str):
    """Track outcome of a prediction."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        today = date.today().isoformat()
        c.execute("""
            INSERT INTO cognition_reviews (timestamp, date, prediction_id, result, analysis, agent)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            today,
            prediction_id,
            result,
            analysis,
            agent
        ))

        conn.commit()
        conn.close()

        logger.debug(f"Prediction review saved: {prediction_id} → {result}")

    except Exception as e:
        logger.error(f"Failed to save prediction review: {e}")


def get_today_prediction_reviews() -> list:
    """Get all prediction reviews from today."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        today = date.today().isoformat()
        rows = c.execute("""
            SELECT * FROM cognition_reviews
            WHERE date = ?
            ORDER BY timestamp ASC
        """, (today,)).fetchall()

        conn.close()
        return [dict(r) for r in rows]

    except Exception as e:
        logger.warning(f"Failed to load prediction reviews: {e}")
        return []


# ════════════════════════════════════════════════════════════
#   MEMORY COMPRESSION
# ════════════════════════════════════════════════════════════

def compress_cognition_memory(keep_cycles: int = 20):
    """
    Compress old cognition data. Keep only recent cycles.
    Summarize older cycles into meta-observations.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        # Get total cycle count
        total = c.execute("SELECT COUNT(*) FROM cognition_cycles").fetchone()[0]

        if total > keep_cycles:
            # Delete oldest entries beyond limit
            c.execute("""
                DELETE FROM cognition_cycles
                WHERE id NOT IN (
                    SELECT id FROM cognition_cycles
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (keep_cycles,))

            conn.commit()
            logger.info(f"Compressed cognition memory: kept {keep_cycles} cycles")

        conn.close()

    except Exception as e:
        logger.warning(f"Failed to compress cognition memory: {e}")


# ════════════════════════════════════════════════════════════
#   DAILY REFLECTION STORAGE
# ════════════════════════════════════════════════════════════

def save_daily_cognition_reflection(reflection: dict):
    """Save final daily cognition reflection."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        c = conn.cursor()

        today = date.today().isoformat()
        c.execute("""
            INSERT OR REPLACE INTO cognition_daily_reflections
            (date, cognition_summary, strongest_patterns, failed_assumptions,
             regime_behavior, unexpected_anomalies, next_day_watch_themes, unresolved_questions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            reflection.get("cognition_summary", ""),
            json.dumps(reflection.get("strongest_patterns", [])),
            json.dumps(reflection.get("failed_assumptions", [])),
            reflection.get("regime_behavior", ""),
            json.dumps(reflection.get("unexpected_anomalies", [])),
            json.dumps(reflection.get("next_day_watch_themes", [])),
            json.dumps(reflection.get("unresolved_questions", [])),
        ))

        conn.commit()
        conn.close()

        logger.info(f"Daily cognition reflection saved: {today}")

    except Exception as e:
        logger.error(f"Failed to save daily cognition reflection: {e}")


def get_today_daily_reflection() -> Optional[dict]:
    """Get today's daily cognition reflection."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        today = date.today().isoformat()
        row = c.execute("""
            SELECT * FROM cognition_daily_reflections
            WHERE date = ?
        """, (today,)).fetchone()

        conn.close()

        if not row:
            return None

        r = dict(row)
        # Decompress JSON fields
        r["strongest_patterns"] = json.loads(r.get("strongest_patterns", "[]"))
        r["failed_assumptions"] = json.loads(r.get("failed_assumptions", "[]"))
        r["unexpected_anomalies"] = json.loads(r.get("unexpected_anomalies", "[]"))
        r["next_day_watch_themes"] = json.loads(r.get("next_day_watch_themes", "[]"))
        r["unresolved_questions"] = json.loads(r.get("unresolved_questions", "[]"))

        return r

    except Exception as e:
        logger.warning(f"Failed to load today's daily reflection: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   INITIALIZATION
# ════════════════════════════════════════════════════════════

_init_cognition_db()
