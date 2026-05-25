# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/state_manager.py — Persistence & Crash Recovery
#   Changes: target_price, trailing_sl, kotak_sl_order_id
#   added to trades table. log_war_room_response signature
#   aligned with base_agent.py. Migration-safe (no data loss).
# ============================================================

import sqlite3
import json
import logging
import os
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH        = "data/alcosoft.db"
POSITIONS_PATH = "data/positions.json"
BRIEFING_PATH  = "data/session_briefing.json"

os.makedirs("data", exist_ok=True)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    """Safe migration — adds column only if it doesn't exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass  # Column already exists


def initialize_db():
    """
    Creates tables if missing.
    Runs column migrations safely — existing data is never lost.
    """
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                trading_symbol      TEXT,
                action              TEXT NOT NULL,
                quantity            INTEGER NOT NULL,
                entry_price         REAL,
                exit_price          REAL,
                stop_loss           REAL,
                trailing_sl         REAL,
                target_price        REAL,
                kotak_order_id      TEXT,
                kotak_sl_order_id   TEXT,
                pnl                 REAL,
                status              TEXT NOT NULL,
                strategy            TEXT,
                confidence          INTEGER,
                trading_mode        TEXT,
                entry_time          TEXT,
                exit_time           TEXT,
                notes               TEXT
            );

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

            CREATE TABLE IF NOT EXISTS war_room_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                round_number INTEGER,
                agent        TEXT,
                symbol       TEXT,
                verdict      TEXT,
                confidence   INTEGER,
                reasons      TEXT,
                concern      TEXT
            );
        """)

        # ── Safe migrations (existing DB users) ──────────────
        _add_column_if_missing(conn, "trades", "trading_symbol",    "TEXT")
        _add_column_if_missing(conn, "trades", "trailing_sl",       "REAL")
        _add_column_if_missing(conn, "trades", "target_price",      "REAL")
        _add_column_if_missing(conn, "trades", "kotak_sl_order_id", "TEXT")

    logger.info("✅ Database initialized.")


# ── Position Management ───────────────────────────────────────
def save_open_position(trade_data: dict):
    """Called immediately when a BUY order is placed."""
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO trades (
                date, symbol, trading_symbol, action, quantity,
                entry_price, stop_loss, trailing_sl, target_price,
                status, strategy, confidence, trading_mode,
                kotak_order_id, kotak_sl_order_id, entry_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d"),
            trade_data["symbol"],
            trade_data.get("trading_symbol", trade_data["symbol"]),
            "BUY",
            trade_data["quantity"],
            trade_data["entry_price"],
            trade_data.get("stop_loss"),
            trade_data.get("stop_loss"),       # trailing_sl starts = stop_loss
            trade_data.get("target_price"),
            "OPEN",
            trade_data.get("strategy", ""),
            trade_data.get("confidence", 0),
            os.getenv("TRADING_MODE", "PAPER"),
            trade_data.get("order_id", ""),
            trade_data.get("sl_order_id", ""),
            now,
        ))
        trade_data["db_id"] = cursor.lastrowid

    _update_positions_json()
    logger.info(
        f"Position saved: {trade_data['symbol']} x{trade_data['quantity']} "
        f"| Target: ₹{trade_data.get('target_price')} "
        f"| SL: ₹{trade_data.get('stop_loss')}"
    )


def close_position(symbol: str, exit_price: float, reason: str = "SIGNAL"):
    """Called when SELL executes. Calculates P&L."""
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM trades
            WHERE symbol = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()

        if not row:
            logger.warning(f"No open position for {symbol}")
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
    logger.info(
        f"Position closed: {symbol} | "
        f"₹{exit_price} | P&L: ₹{pnl:.2f} | {reason}"
    )


def update_trailing_sl(symbol: str, new_trailing_sl: float):
    """
    Moves trailing SL up as price rises.
    Called by order_executor on every tick for open positions.
    """
    with _get_conn() as conn:
        conn.execute("""
            UPDATE trades
            SET trailing_sl = ?
            WHERE symbol = ? AND status = 'OPEN'
        """, (new_trailing_sl, symbol))

    _update_positions_json()


def update_sl_order_id(symbol: str, sl_order_id: str):
    """Saves Kotak's SL-M order ID after it's placed."""
    with _get_conn() as conn:
        conn.execute("""
            UPDATE trades
            SET kotak_sl_order_id = ?
            WHERE symbol = ? AND status = 'OPEN'
        """, (sl_order_id, symbol))


def get_open_positions() -> list[dict]:
    """Returns all open positions. Called for crash recovery too."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades WHERE status = 'OPEN'
        """).fetchall()
        return [dict(row) for row in rows]


def _update_positions_json():
    positions = get_open_positions()
    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


# ── Daily Stats ───────────────────────────────────────────────
def _update_daily_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT pnl, status FROM trades
            WHERE date = ? AND status IN ('CLOSED', 'STOPPED')
        """, (today,)).fetchall()

        total   = len(rows)
        winners = sum(1 for r in rows if r["pnl"] and r["pnl"] > 0)
        losers  = total - winners
        gross   = sum(r["pnl"] for r in rows if r["pnl"])

        conn.execute("""
            INSERT INTO daily_stats
            (date, total_trades, winning_trades, losing_trades, gross_pnl)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_trades   = excluded.total_trades,
                winning_trades = excluded.winning_trades,
                losing_trades  = excluded.losing_trades,
                gross_pnl      = excluded.gross_pnl
        """, (today, total, winners, losers, gross))


def get_today_stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM daily_stats WHERE date = ?
        """, (today,)).fetchone()
        return dict(row) if row else {
            "date": today, "total_trades": 0,
            "winning_trades": 0, "losing_trades": 0, "gross_pnl": 0.0
        }


def get_today_gross_pnl() -> float:
    return get_today_stats().get("gross_pnl", 0.0)


def get_recent_trades(days: int = 7) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE status IN ('CLOSED', 'STOPPED')
            ORDER BY id DESC LIMIT ?
        """, (days * 10,)).fetchall()
        return [dict(row) for row in rows]


# ── War Room Log ──────────────────────────────────────────────
def log_war_room_response(
    agent:        str,
    symbol:       str,
    round_number: int,
    verdict:      str,
    confidence:   int,
    reasons:      list,
    concern:      str,
):
    """
    Aligned with base_agent.py — accepts reasons as list,
    stores as JSON string in DB.
    """
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
            json.dumps(reasons), concern,
        ))


# ── Session Briefing ──────────────────────────────────────────
def save_briefing(briefing: dict):
    with open(BRIEFING_PATH, "w") as f:
        json.dump(briefing, f, indent=2)
    logger.info("Session briefing updated.")


def load_briefing() -> dict | None:
    if not os.path.exists(BRIEFING_PATH):
        return None
    with open(BRIEFING_PATH, "r") as f:
        return json.load(f)


# ── Crash Recovery ────────────────────────────────────────────
def recover_state() -> dict:
    open_positions = get_open_positions()
    briefing       = load_briefing()

    if open_positions:
        logger.warning(
            f"⚠️ CRASH RECOVERY: {len(open_positions)} open position(s): "
            f"{[p['symbol'] for p in open_positions]}"
        )
    else:
        logger.info("✅ No open positions found. Clean startup.")

    return {
        "open_positions":       open_positions,
        "open_position_count":  len(open_positions),
        "last_briefing":        briefing,
        "has_active_briefing":  briefing is not None,
    }


if __name__ == "__main__":
    print("Testing database...")
    initialize_db()
    print("DB initialized!")
    state = recover_state()
    print(f"Open positions: {state['open_position_count']}")
    stats = get_today_stats()
    print(f"Today's stats: {stats}")