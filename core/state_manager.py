# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/state_manager.py — Persistence & Crash Recovery
#   SQLite for trade history | JSON for fast position reads
#   If laptop dies mid-session, this brings everything back.
# ============================================================

import sqlite3
import json
import logging
import os
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────
DB_PATH            = "data/alcosoft.db"
POSITIONS_PATH     = "data/positions.json"
BRIEFING_PATH      = "data/session_briefing.json"

os.makedirs("data", exist_ok=True)


# ── Database Connection ───────────────────────────────────────
@contextmanager
def _get_conn():
    """Context manager — auto-commits and closes connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # access columns by name
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


# ── DB Setup: Run Once on First Launch ───────────────────────
def initialize_db():
    """
    Creates all tables if they don't exist.
    Safe to call every startup — won't overwrite existing data.
    """
    with _get_conn() as conn:
        conn.executescript("""
            -- All completed + open trades
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                action          TEXT NOT NULL,   -- BUY or SELL
                quantity        INTEGER NOT NULL,
                entry_price     REAL,
                exit_price      REAL,
                stop_loss       REAL,
                pnl             REAL,            -- filled on exit
                status          TEXT NOT NULL,   -- OPEN, CLOSED, STOPPED
                strategy        TEXT,            -- e.g. Swing_Momentum
                confidence      INTEGER,         -- war room confidence score
                trading_mode    TEXT,            -- PAPER or LIVE
                kotak_order_id  TEXT,
                entry_time      TEXT,
                exit_time       TEXT,
                notes           TEXT
            );

            -- Daily summary for reflection loop
            CREATE TABLE IF NOT EXISTS daily_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT UNIQUE NOT NULL,
                total_trades    INTEGER DEFAULT 0,
                winning_trades  INTEGER DEFAULT 0,
                losing_trades   INTEGER DEFAULT 0,
                gross_pnl       REAL DEFAULT 0.0,
                capital_start   REAL,
                capital_end     REAL,
                war_room_calls  INTEGER DEFAULT 0,
                notes           TEXT
            );

            -- War room debate log (for reflection)
            CREATE TABLE IF NOT EXISTS war_room_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                round_number INTEGER,
                agent        TEXT,
                symbol       TEXT,
                verdict      TEXT,
                confidence   INTEGER,
                reasons      TEXT,    -- JSON array stored as string
                concern      TEXT
            );
        """)
    logger.info("✅ Database initialized.")


# ── Position Management (Crash Recovery Core) ─────────────────
def save_open_position(trade_data: dict):
    """
    Called immediately when a BUY order is placed.
    Saves to both SQLite (permanent log) and positions.json (fast read).
    """
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO trades (
                date, symbol, action, quantity, entry_price,
                stop_loss, status, strategy, confidence,
                trading_mode, kotak_order_id, entry_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d"),
            trade_data["symbol"],
            "BUY",
            trade_data["quantity"],
            trade_data["entry_price"],
            trade_data.get("stop_loss"),
            "OPEN",
            trade_data.get("strategy", ""),
            trade_data.get("confidence", 0),
            os.getenv("TRADING_MODE", "PAPER"),
            trade_data.get("order_id", ""),
            now,
        ))
        trade_data["db_id"] = cursor.lastrowid

    # Also write to fast-access JSON
    _update_positions_json()
    logger.info(f"Position saved: {trade_data['symbol']} x{trade_data['quantity']}")


def close_position(symbol: str, exit_price: float, reason: str = "SIGNAL"):
    """
    Called when a SELL order executes or stop-loss is hit.
    Calculates P&L and marks trade as CLOSED.
    """
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        # Find the open trade
        row = conn.execute("""
            SELECT * FROM trades
            WHERE symbol = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()

        if not row:
            logger.warning(f"No open position found for {symbol}")
            return

        pnl    = (exit_price - row["entry_price"]) * row["quantity"]
        status = "STOPPED" if reason == "STOPLOSS" else "CLOSED"

        conn.execute("""
            UPDATE trades
            SET exit_price = ?, pnl = ?, status = ?,
                exit_time = ?, notes = ?
            WHERE id = ?
        """, (exit_price, pnl, status, now, reason, row["id"]))

    _update_positions_json()
    _update_daily_stats()
    logger.info(f"Position closed: {symbol} | P&L: ₹{pnl:.2f} | Reason: {reason}")


def get_open_positions() -> list[dict]:
    """
    Returns all currently open positions.
    Called on startup for crash recovery — restores in-memory state.
    """
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades WHERE status = 'OPEN'
        """).fetchall()
        return [dict(row) for row in rows]


def _update_positions_json():
    """Syncs open positions to JSON for fast reads by strategy.py."""
    positions = get_open_positions()
    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


# ── Daily Stats ───────────────────────────────────────────────
def _update_daily_stats():
    """Recalculates and saves today's stats. Called after every trade close."""
    today = datetime.now().strftime("%Y-%m-%d")

    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT pnl, status FROM trades
            WHERE date = ? AND status IN ('CLOSED', 'STOPPED')
        """, (today,)).fetchall()

        total   = len(rows)
        winners = sum(1 for r in rows if r["pnl"] and r["pnl"] > 0)
        losers  = sum(1 for r in rows if r["pnl"] and r["pnl"] <= 0)
        gross   = sum(r["pnl"] for r in rows if r["pnl"])

        conn.execute("""
            INSERT INTO daily_stats (date, total_trades, winning_trades, losing_trades, gross_pnl)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_trades   = excluded.total_trades,
                winning_trades = excluded.winning_trades,
                losing_trades  = excluded.losing_trades,
                gross_pnl      = excluded.gross_pnl
        """, (today, total, winners, losers, gross))


def get_today_stats() -> dict:
    """Returns today's P&L summary. Used by dashboard and reflection loop."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM daily_stats WHERE date = ?
        """, (today,)).fetchone()
        return dict(row) if row else {
            "date": today, "total_trades": 0,
            "winning_trades": 0, "losing_trades": 0, "gross_pnl": 0.0
        }


def get_recent_trades(days: int = 7) -> list[dict]:
    """Returns last N days of closed trades. Used by reflection loop."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE status IN ('CLOSED', 'STOPPED')
            ORDER BY id DESC
            LIMIT ?
        """, (days * 10,)).fetchall()
        return [dict(row) for row in rows]


# ── War Room Log ──────────────────────────────────────────────
def log_war_room_response(
    agent: str, symbol: str, round_number: int,
    verdict: str, confidence: int,
    reasons: list, concern: str
):
    import json as _json
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO war_room_log
            (timestamp, round_number, agent, symbol,
             verdict, confidence, reasons, concern)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            round_number, agent, symbol,
            verdict, confidence,
            _json.dumps(reasons), concern,
        ))


# ── Session Briefing (JSON Bridge) ───────────────────────────
def save_briefing(briefing: dict):
    """War room calls this after every debate. strategy.py reads it."""
    with open(BRIEFING_PATH, "w") as f:
        json.dump(briefing, f, indent=2)
    logger.info("Session briefing updated.")


def load_briefing() -> dict | None:
    """strategy.py calls this to get current approved stocks + bias."""
    if not os.path.exists(BRIEFING_PATH):
        logger.warning("No session briefing found. War room hasn't run yet.")
        return None
    with open(BRIEFING_PATH, "r") as f:
        return json.load(f)


# ── Crash Recovery ────────────────────────────────────────────
def recover_state() -> dict:
    """
    Called at every system startup.
    Returns a summary of what was running before shutdown.
    """
    open_positions = get_open_positions()
    briefing       = load_briefing()

    summary = {
        "open_positions":       open_positions,
        "open_position_count":  len(open_positions),
        "last_briefing":        briefing,
        "has_active_briefing":  briefing is not None,
    }

    if open_positions:
        logger.warning(
            f"⚠️ CRASH RECOVERY: Found {len(open_positions)} open position(s): "
            f"{[p['symbol'] for p in open_positions]}"
        )
    else:
        logger.info("✅ No open positions found. Clean startup.")

    return summary


if __name__ == "__main__":
    print("Testing database...")
    initialize_db()
    print("DB initialized!")
    
    state = recover_state()
    print(f"Open positions: {state['open_position_count']}")
    
    stats = get_today_stats()
    print(f"Today's stats: {stats}")