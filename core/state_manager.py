import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from core.safe_io import atomic_write_json, safe_float, safe_int, safe_read_json


logger = logging.getLogger(__name__)

DB_PATH = "data/alcosoft.db"
POSITIONS_PATH = "data/positions.json"
BRIEFING_PATH = "data/session_briefing.json"
LEGACY_AGENT_TABLE = "war" + "_room_log"

FALLBACK_BRIEFING = {
    "generated_at": None,
    "session_type": "SAFE_FALLBACK",
    "market_bias": "NEUTRAL",
    "approved_stocks": [],
    "watchlist": [],
    "avoid_list": [],
}

os.makedirs("data", exist_ok=True)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            logger.warning("Column migration skipped for %s.%s: %s", table, column, exc)


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _migrate_agent_decision_log(conn):
    if not _table_exists(conn, LEGACY_AGENT_TABLE):
        return

    try:
        existing = conn.execute("SELECT COUNT(*) FROM agent_decision_log").fetchone()[0]
        if existing == 0:
            conn.execute(f"""
                INSERT INTO agent_decision_log
                (timestamp, round_number, agent, symbol, verdict, confidence, reasons, concern)
                SELECT timestamp, round_number, agent, symbol, verdict, confidence, reasons, concern
                FROM {LEGACY_AGENT_TABLE}
            """)
        conn.execute(f"DROP TABLE {LEGACY_AGENT_TABLE}")
        logger.info("Legacy agent decision table migrated.")
    except sqlite3.DatabaseError as exc:
        logger.warning("Legacy agent decision migration skipped: %s", exc)


def _repair_invalid_open_positions(conn) -> int:
    now = datetime.now().isoformat()
    cursor = conn.execute("""
        UPDATE trades
        SET status = 'INVALID',
            exit_time = COALESCE(exit_time, ?),
            notes = CASE
                WHEN notes IS NULL OR notes = ''
                    THEN 'INVALID_NON_POSITIVE_QUANTITY'
                ELSE notes || '|INVALID_NON_POSITIVE_QUANTITY'
            END
        WHERE status = 'OPEN' AND quantity <= 0
    """, (now,))
    repaired = cursor.rowcount or 0
    if repaired:
        logger.error("Repaired %d invalid OPEN position(s) with non-positive quantity.", repaired)
    return repaired


def initialize_db():
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
                agent_decision_calls INTEGER DEFAULT 0,
                notes           TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_decision_log (
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

        _add_column_if_missing(conn, "trades", "trading_symbol", "TEXT")
        _add_column_if_missing(conn, "trades", "trailing_sl", "REAL")
        _add_column_if_missing(conn, "trades", "target_price", "REAL")
        _add_column_if_missing(conn, "trades", "kotak_sl_order_id", "TEXT")
        _add_column_if_missing(conn, "daily_stats", "agent_decision_calls", "INTEGER DEFAULT 0")
        _migrate_agent_decision_log(conn)
        _repair_invalid_open_positions(conn)

    logger.info("Database initialized.")


def _sanitize_position(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data["symbol"] = str(data.get("symbol", "")).strip().upper()
    data["trading_symbol"] = data.get("trading_symbol") or data["symbol"]
    data["quantity"] = safe_int(data.get("quantity"), 0)
    for key in ("entry_price", "exit_price", "stop_loss", "trailing_sl", "target_price", "pnl"):
        if key in data and data.get(key) is not None:
            data[key] = safe_float(data.get(key), 0.0)
    data["confidence"] = safe_int(data.get("confidence"), 0)
    return data


def save_open_position(trade_data: dict):
    now = datetime.now().isoformat()
    symbol = str(trade_data.get("symbol", "")).strip().upper()
    if not symbol:
        logger.error("Cannot save open position without symbol: %s", trade_data)
        return

    quantity = safe_int(trade_data.get("quantity"), 0)
    if quantity <= 0:
        logger.error("Refusing to save invalid open position for %s: quantity=%r", symbol, trade_data.get("quantity"))
        return
    entry_price = safe_float(trade_data.get("entry_price"), 0.0)
    stop_loss = safe_float(trade_data.get("stop_loss"), 0.0)
    trailing_sl = safe_float(trade_data.get("trailing_sl", stop_loss), stop_loss)
    target_price = safe_float(trade_data.get("target_price"), 0.0)

    with _get_conn() as conn:
        existing = conn.execute("""
            SELECT id FROM trades
            WHERE symbol = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()

        if existing:
            conn.execute("""
                UPDATE trades
                SET trading_symbol = ?, quantity = ?, entry_price = ?,
                    stop_loss = ?, trailing_sl = ?, target_price = ?,
                    strategy = ?, confidence = ?, trading_mode = ?,
                    kotak_order_id = ?, kotak_sl_order_id = ?, notes = ?
                WHERE id = ?
            """, (
                trade_data.get("trading_symbol", symbol),
                quantity,
                entry_price,
                stop_loss,
                trailing_sl,
                target_price,
                trade_data.get("strategy", ""),
                safe_int(trade_data.get("confidence"), 0),
                os.getenv("TRADING_MODE", "PAPER"),
                trade_data.get("order_id", ""),
                trade_data.get("sl_order_id", ""),
                trade_data.get("notes", ""),
                existing["id"],
            ))
            trade_data["db_id"] = existing["id"]
        else:
            cursor = conn.execute("""
                INSERT INTO trades (
                    date, symbol, trading_symbol, action, quantity,
                    entry_price, stop_loss, trailing_sl, target_price,
                    status, strategy, confidence, trading_mode,
                    kotak_order_id, kotak_sl_order_id, entry_time, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                symbol,
                trade_data.get("trading_symbol", symbol),
                "BUY",
                quantity,
                entry_price,
                stop_loss,
                trailing_sl,
                target_price,
                "OPEN",
                trade_data.get("strategy", ""),
                safe_int(trade_data.get("confidence"), 0),
                os.getenv("TRADING_MODE", "PAPER"),
                trade_data.get("order_id", ""),
                trade_data.get("sl_order_id", ""),
                now,
                trade_data.get("notes", ""),
            ))
            trade_data["db_id"] = cursor.lastrowid

    _update_positions_json()
    logger.info("Position saved: %s x%s | Target: %s | SL: %s", symbol, quantity, target_price, stop_loss)


def recover_open_position(trade_data: dict) -> bool:
    trade = dict(trade_data)
    trade.setdefault("order_id", "BROKER-RECOVERED")
    trade.setdefault("notes", "Recovered from broker reconciliation")
    save_open_position(trade)
    return True


def update_open_position_from_broker(symbol: str, updates: dict) -> bool:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return False

    allowed = {
        "trading_symbol": str,
        "quantity": int,
        "entry_price": float,
        "stop_loss": float,
        "trailing_sl": float,
        "target_price": float,
        "strategy": str,
        "kotak_order_id": str,
        "kotak_sl_order_id": str,
        "notes": str,
    }
    fields = []
    values = []
    for key, caster in allowed.items():
        if key not in updates:
            continue
        value = updates[key]
        if caster is int:
            value = safe_int(value, 0)
        elif caster is float:
            value = safe_float(value, 0.0)
        else:
            value = str(value or "")
        fields.append(f"{key} = ?")
        values.append(value)

    if not fields:
        return False

    values.append(symbol)
    with _get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE trades SET {', '.join(fields)} WHERE symbol = ? AND status = 'OPEN'",
            tuple(values),
        )

    _update_positions_json()
    return cursor.rowcount > 0


def mark_position_reconciled_closed(symbol: str, exit_price: float, reason: str) -> bool:
    return close_position(symbol, exit_price, reason)


def close_position(symbol: str, exit_price: float, reason: str = "SIGNAL") -> bool:
    now = datetime.now().isoformat()
    symbol = str(symbol or "").strip().upper()
    exit_price = safe_float(exit_price, 0.0)
    if not symbol:
        return False

    with _get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM trades
            WHERE symbol = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()

        if not row:
            logger.warning("No open position for %s", symbol)
            return False

        pnl = (exit_price - safe_float(row["entry_price"], 0.0)) * safe_int(row["quantity"], 0)
        status = "STOPPED" if reason == "STOPLOSS" else "CLOSED"
        conn.execute("""
            UPDATE trades
            SET exit_price = ?, pnl = ?, status = ?,
                exit_time = ?, notes = ?
            WHERE id = ?
        """, (exit_price, pnl, status, now, reason, row["id"]))

    _update_positions_json()
    _update_daily_stats()
    logger.info("Position closed: %s | %s | P&L: %.2f | %s", symbol, exit_price, pnl, reason)
    return True


def update_trailing_sl(symbol: str, new_trailing_sl: float):
    with _get_conn() as conn:
        conn.execute("""
            UPDATE trades
            SET trailing_sl = ?
            WHERE symbol = ? AND status = 'OPEN'
        """, (safe_float(new_trailing_sl, 0.0), str(symbol or "").strip().upper()))

    _update_positions_json()


def update_sl_order_id(symbol: str, sl_order_id: str):
    with _get_conn() as conn:
        conn.execute("""
            UPDATE trades
            SET kotak_sl_order_id = ?
            WHERE symbol = ? AND status = 'OPEN'
        """, (sl_order_id or "", str(symbol or "").strip().upper()))

    _update_positions_json()


def get_open_positions() -> list[dict]:
    try:
        with _get_conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status = 'OPEN' AND quantity > 0").fetchall()
            return [_sanitize_position(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        logger.error("Open position read failed: %s", exc)
        return []


def _update_positions_json():
    positions = get_open_positions()
    atomic_write_json(POSITIONS_PATH, positions, label="positions snapshot", log=logger)


def _update_daily_stats():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT pnl, status FROM trades
                WHERE date = ? AND status IN ('CLOSED', 'STOPPED')
            """, (today,)).fetchall()

            total = len(rows)
            winners = sum(1 for row in rows if safe_float(row["pnl"], 0.0) > 0)
            losers = total - winners
            gross = sum(safe_float(row["pnl"], 0.0) for row in rows)

            existing = conn.execute(
                "SELECT capital_start FROM daily_stats WHERE date = ?",
                (today,),
            ).fetchone()
            capital_start = existing["capital_start"] if existing else None
            if not capital_start:
                from core.order_executor import _get_available_capital
                capital_start = _get_available_capital() + gross

            from core.order_executor import _get_available_capital
            capital_end = _get_available_capital()

            conn.execute("""
                INSERT INTO daily_stats
                (date, total_trades, winning_trades, losing_trades, gross_pnl, capital_start, capital_end)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_trades   = excluded.total_trades,
                    winning_trades = excluded.winning_trades,
                    losing_trades  = excluded.losing_trades,
                    gross_pnl      = excluded.gross_pnl,
                    capital_end    = excluded.capital_end
            """, (today, total, winners, losers, gross, capital_start, capital_end))
    except Exception as exc:
        logger.error("Daily stats update failed: %s", exc, exc_info=True)


def get_today_stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    fallback = {
        "date": today,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "gross_pnl": 0.0,
    }
    try:
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,)).fetchone()
            return dict(row) if row else fallback
    except sqlite3.DatabaseError as exc:
        logger.error("Today stats read failed: %s", exc)
        return fallback


def get_today_gross_pnl() -> float:
    return safe_float(get_today_stats().get("gross_pnl"), 0.0)


def get_recent_trades(days: int = 7) -> list[dict]:
    try:
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE status IN ('CLOSED', 'STOPPED')
                ORDER BY id DESC LIMIT ?
            """, (safe_int(days, 7) * 10,)).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        logger.error("Recent trades read failed: %s", exc)
        return []


def log_agent_decision(
    agent: str,
    symbol: str,
    round_number: int,
    verdict: str,
    confidence: int,
    reasons: list,
    concern: str,
):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO agent_decision_log
            (timestamp, round_number, agent, symbol, verdict, confidence, reasons, concern)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            safe_int(round_number, 0),
            agent,
            str(symbol or "").strip().upper(),
            verdict,
            safe_int(confidence, 0),
            json.dumps(reasons if isinstance(reasons, list) else [str(reasons)]),
            concern or "",
        ))


def load_agent_decisions_today(limit: int = 10) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT agent, symbol, verdict, confidence,
                       reasons, concern, timestamp, round_number
                FROM agent_decision_log
                WHERE timestamp LIKE ?
                ORDER BY id DESC LIMIT ?
            """, (f"{today}%", safe_int(limit, 10))).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        logger.error("Agent decision log read failed: %s", exc)
        return []


def validate_briefing(briefing: dict | None) -> tuple[bool, str]:
    """
    COMPREHENSIVE BRIEFING VALIDATION
    
    Validates briefing structure, content, and safety markers.
    Returns (is_valid: bool, reason: str).
    
    Rejects:
    - None or non-dict
    - Empty (no approved_stocks AND no watchlist)
    - TEST_* session types (test briefings)
    - Malformed stock lists
    - Marked as placeholder
    
    [BRIEFING] logs all validation decisions.
    """
    if briefing is None:
        logger.warning("[BRIEFING] Validation REJECTED: Briefing is None")
        return False, "Briefing is None"
    
    if not isinstance(briefing, dict):
        logger.warning(f"[BRIEFING] Validation REJECTED: Invalid type {type(briefing).__name__}")
        return False, f"Invalid briefing type: {type(briefing).__name__}"
    
    # Reject placeholder markers
    if briefing.get("do_not_use_for_trading") is True:
        session_type = briefing.get("session_type", "UNKNOWN")
        logger.warning(f"[BRIEFING] Validation REJECTED: Placeholder briefing ({session_type})")
        return False, f"Briefing marked as placeholder ({session_type})"
    
    # Reject test briefings
    session_type = briefing.get("session_type", "")
    if isinstance(session_type, str) and session_type.startswith("TEST"):
        logger.warning(f"[BRIEFING] Validation REJECTED: Test briefing ({session_type})")
        return False, f"Test briefing found ({session_type}) - must regenerate"
    
    # Validate structure
    approved = briefing.get("approved_stocks", [])
    watchlist = briefing.get("watchlist", [])
    
    if not isinstance(approved, list):
        logger.warning(f"[BRIEFING] Validation REJECTED: approved_stocks is not a list")
        return False, "approved_stocks must be a list"
    
    if not isinstance(watchlist, list):
        logger.warning(f"[BRIEFING] Validation REJECTED: watchlist is not a list")
        return False, "watchlist must be a list"
    
    # Reject empty briefings
    total_stocks = len(approved) + len(watchlist)
    if total_stocks == 0:
        logger.warning(f"[BRIEFING] Validation REJECTED: Empty briefing (0 approved + 0 watchlist)")
        return False, "Briefing is empty (no stocks)"
    
    # Validate stock structure (sample check on first few)
    for i, stock in enumerate(approved[:2]):
        if not isinstance(stock, dict):
            logger.warning(f"[BRIEFING] Validation REJECTED: approved_stocks[{i}] is not a dict")
            return False, f"Stock {i} in approved_stocks must be dict"
    
    for i, stock in enumerate(watchlist[:2]):
        if not isinstance(stock, dict):
            logger.warning(f"[BRIEFING] Validation REJECTED: watchlist[{i}] is not a dict")
            return False, f"Stock {i} in watchlist must be dict"
    
    logger.info(f"[BRIEFING] Validation PASSED: {len(approved)} approved + {len(watchlist)} watchlist ({session_type})")
    return True, f"Valid briefing ({len(approved)} approved + {len(watchlist)} watchlist)"


def is_briefing_safe_for_trading(briefing: dict | None) -> tuple[bool, str]:
    """
    FINAL SAFETY GATE FOR TRADING
    
    Comprehensive check before allowing trading to start.
    Returns (safe: bool, reason: str).
    
    Uses validate_briefing() plus additional safety checks.
    """
    is_valid, reason = validate_briefing(briefing)
    if not is_valid:
        logger.error(f"[BRIEFING] Trading blocked: {reason}")
        return False, reason
    
    # Additional safety gate: must have stocks for trading
    if briefing is None:
        logger.error("[BRIEFING] Trading blocked: Safety gate - briefing is None after validation")
        return False, "Briefing is None"
    
    approved = len(briefing.get("approved_stocks", []))
    watchlist = len(briefing.get("watchlist", []))
    
    if approved == 0 and watchlist == 0:
        logger.error("[BRIEFING] Trading blocked: Safety gate - no stocks available")
        return False, "No stocks available for trading"
    
    logger.info(f"[BRIEFING] Safety gate PASSED: Ready for trading")
    return True, "Safe for trading"


def save_briefing(briefing: dict) -> bool:
    if not isinstance(briefing, dict):
        logger.error("[BRIEFING] Save FAILED: Invalid type %s (expected dict)", type(briefing).__name__)
        return False
    
    # Write to disk
    logger.info(f"[BRIEFING] Saving to {BRIEFING_PATH}...")
    ok = atomic_write_json(BRIEFING_PATH, briefing, label="session briefing", log=logger)
    
    if not ok:
        logger.error(f"[BRIEFING] Save FAILED: atomic_write_json() returned False")
        return False
    
    # POST-WRITE VERIFICATION: Verify file actually exists
    if not os.path.exists(BRIEFING_PATH):
        logger.error(f"[BRIEFING] Save FAILED: File does not exist after write")
        return False
    
    # POST-READ VERIFICATION: Verify we can read it back
    try:
        with open(BRIEFING_PATH, 'r') as f:
            saved_briefing = json.load(f)
        logger.info(f"[BRIEFING] Saved and verified: {BRIEFING_PATH}")
        logger.info(f"[BRIEFING]   - Approved: {len(saved_briefing.get('approved_stocks', []))}")
        logger.info(f"[BRIEFING]   - Watchlist: {len(saved_briefing.get('watchlist', []))}")
        
        # Invalidate strategy cache so new briefing is used immediately
        try:
            from core import strategy
            strategy._briefing_cache = None
            strategy._briefing_cache_time = 0.0
            logger.debug("[BRIEFING] Strategy cache invalidated for immediate update")
        except Exception:
            pass  # Strategy not loaded yet (e.g., during startup)
        
        return True
    except Exception as e:
        logger.error(f"[BRIEFING] Save FAILED: Could not read back saved file: {e}")
        return False


def ensure_briefing_exists() -> dict:
    """
    STARTUP: Ensure session_briefing.json exists.
    
    If missing: Create placeholder (marked as DO NOT USE FOR TRADING).
    Returns the briefing object.
    
    Placeholder is clearly marked so validation functions reject it.
    Screener must be run to replace placeholder with valid briefing.
    
    [BRIEFING] logs creation with explicit warning.
    """
    if not os.path.exists(BRIEFING_PATH):
        logger.warning(f"[BRIEFING] File missing at startup - creating placeholder...")
        placeholder_briefing = {
            "generated_at": datetime.now().isoformat(),
            "session_type": "PLACEHOLDER_AWAITING_SCREENER",
            "market_bias": "NEUTRAL",
            "approved_stocks": [],
            "watchlist": [],
            "avoid_list": [],
            "do_not_use_for_trading": True,  # EXPLICIT MARKER
        }
        try:
            atomic_write_json(BRIEFING_PATH, placeholder_briefing)
            logger.warning(f"[BRIEFING] Created Placeholder: {BRIEFING_PATH}")
            logger.warning(f"[BRIEFING]   ⚠️  PLACEHOLDER BRIEFING - SCREENER MUST RUN")
            return placeholder_briefing
        except Exception as e:
            logger.error(f"[BRIEFING] Failed to create placeholder: {e}")
            fallback = dict(FALLBACK_BRIEFING)
            fallback["do_not_use_for_trading"] = True
            fallback["session_type"] = "FALLBACK_ERROR"
            return fallback
    
    return load_briefing() or dict(FALLBACK_BRIEFING)


def load_briefing() -> dict | None:
    if not os.path.exists(BRIEFING_PATH):
        logger.warning(f"[BRIEFING] File not found - cannot load")
        return None
    
    logger.info(f"[BRIEFING] Loading from {BRIEFING_PATH}...")
    briefing = safe_read_json(
        BRIEFING_PATH,
        dict(FALLBACK_BRIEFING),
        expected_type=dict,
        label="session briefing",
        log=logger,
    )
    
    if not isinstance(briefing.get("approved_stocks"), list):
        briefing["approved_stocks"] = []
    if not isinstance(briefing.get("watchlist"), list):
        briefing["watchlist"] = []
    if not isinstance(briefing.get("avoid_list"), list):
        briefing["avoid_list"] = []
    briefing.setdefault("market_bias", "NEUTRAL")
    briefing.setdefault("session_type", "SAFE_FALLBACK")
    
    # Log what we loaded
    approved_count = len(briefing.get("approved_stocks", []))
    watchlist_count = len(briefing.get("watchlist", []))
    total_count = approved_count + watchlist_count
    session_type = briefing.get("session_type", "UNKNOWN")
    is_placeholder = briefing.get("do_not_use_for_trading", False)
    
    if is_placeholder:
        logger.warning(f"[BRIEFING] Loaded Placeholder ({session_type}) - NOT for trading")
    else:
        logger.info(f"[BRIEFING] Loaded Valid: {approved_count} approved + {watchlist_count} watchlist ({session_type})")
    
    return briefing


def recover_state() -> dict:
    open_positions = get_open_positions()
    briefing = load_briefing()

    if open_positions:
        logger.warning(
            "CRASH RECOVERY: %s open position(s): %s",
            len(open_positions),
            [p["symbol"] for p in open_positions],
        )
    else:
        logger.info("No open positions found. Clean startup.")

    return {
        "open_positions": open_positions,
        "open_position_count": len(open_positions),
        "last_briefing": briefing,
        "has_active_briefing": briefing is not None,
    }


if __name__ == "__main__":
    initialize_db()
    state = recover_state()
    print(f"Open positions: {state['open_position_count']}")
    print(f"Today's stats: {get_today_stats()}")
