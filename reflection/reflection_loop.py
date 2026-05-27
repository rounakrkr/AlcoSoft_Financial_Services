# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/reflection_loop.py — End of Day Learning
#   Runs at 3:35 PM after market close.
#   Owl Alpha reads today's trades + war room logs,
#   finds patterns, suggests tomorrow's adjustments.
# ============================================================

import json
import logging
import os
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv

from core.state_manager import get_today_stats, get_recent_trades

load_dotenv()
logger = logging.getLogger(__name__)

REFLECTIONS_DIR = "data/reflections"
os.makedirs(REFLECTIONS_DIR, exist_ok=True)

# Owl Alpha — OpenAI-compatible endpoint
# Add OWL_BASE_URL to .env if Owl has a custom endpoint
# Leave blank if it's standard OpenAI API format
OWL_BASE_URL = os.getenv("OWL_BASE_URL", None)
OWL_MODEL    = os.getenv("OWL_MODEL", "openai/o1-mini")


# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

def run_reflection_loop():
    """
    Called at 3:35 PM daily.
    Reads today's performance, asks Owl Alpha to reflect,
    saves insights for tomorrow.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"🦉 Owl Alpha reflection starting for {today}...")

    # ── Gather today's data ───────────────────────────────────
    stats         = get_today_stats()
    recent_trades = get_recent_trades(days=1)
    war_room_log  = _load_war_room_log_today()

    # Nothing to reflect on
    if stats["total_trades"] == 0 and not war_room_log:
        logger.info("No trades or war room activity today. Skipping reflection.")
        _save_reflection(today, _empty_reflection(today))
        return

    # ── Build context for Owl ─────────────────────────────────
    context = _build_reflection_context(stats, recent_trades, war_room_log)

    # ── Call Owl Alpha ────────────────────────────────────────
    insights = _call_owl(context)

    if not insights:
        logger.warning("Owl Alpha returned nothing. Saving raw stats only.")
        insights = _fallback_reflection(today, stats)

    insights["date"]        = today
    insights["raw_stats"]   = stats

    # ── Save ──────────────────────────────────────────────────
    _save_reflection(today, insights)
    _update_running_learnings(insights)

    logger.info(
        f"✅ Reflection complete. "
        f"Win rate: {stats.get('winning_trades', 0)}/"
        f"{stats.get('total_trades', 0)} trades."
    )


# ════════════════════════════════════════════════════════════
#   OWL ALPHA API CALL
# ════════════════════════════════════════════════════════════

def _call_owl(context: str) -> dict | None:
    """
    Calls Owl Alpha via OpenRouter Key 3.
    GPT-OSS standby on same key if Owl fails.
    """
    from war_room.agents.base_agent import (
        _call_openrouter, _parse_json, OPENROUTER_KEYS, MODELS
    )

    key = OPENROUTER_KEYS.get("reflection")

    try:
        raw = _call_openrouter(
            key    = key,
            model  = MODELS["reflection"],
            system = _system_prompt(),
            user   = context,
        )
        logger.info("Owl Alpha response received.")

    except Exception as e:
        logger.warning(f"Owl failed: {e}. GPT-OSS standby activating...")
        try:
            raw = _call_openrouter(
                key    = key,
                model  = MODELS["standby"],
                system = _system_prompt(),
                user   = context,
            )
        except Exception as e2:
            logger.error(f"Standby also failed: {e2}")
            return None

    return _parse_json(raw)


def _system_prompt() -> str:
    return """
Owl Alpha — EOD reflection for AlcoSoft.
Use ONLY trade stats and war room log in the user message. Do not invent trades.

JSON only:
{"win_rate_pct":0,"patterns_observed":["max 8 words","max 3"],"what_worked":"one sentence","what_failed":"one sentence","tomorrow_adjustments":["max 10 words","max 3"],"confidence_calibration":"one line","overall_grade":"A-F or N/A","one_line_summary":"one line"}
""".strip()


# ════════════════════════════════════════════════════════════
#   CONTEXT BUILDER
# ════════════════════════════════════════════════════════════

def _build_reflection_context(
    stats: dict,
    trades: list,
    war_room_log: list
) -> str:
    """Builds the full context string for Owl Alpha."""

    # Trade summary
    trade_lines = []
    for t in trades:
        outcome = "WIN" if (t.get("pnl") or 0) > 0 else "LOSS"
        trade_lines.append(
            f"  {t['symbol']} | {t['status']} | "
            f"Entry: ₹{t['entry_price']} | "
            f"Exit: ₹{t.get('exit_price', 'OPEN')} | "
            f"P&L: ₹{t.get('pnl', 0):.2f} | {outcome} | "
            f"Strategy: {t.get('strategy', 'Unknown')}"
        )

    trades_text = "\n".join(trade_lines) if trade_lines else "No trades today."

    # War room summary
    war_lines = []
    for log in war_room_log:
        war_lines.append(
            f"  [{log.get('agent')}] R{log.get('round_number')} | "
            f"{log.get('symbol')} | Verdict: {log.get('verdict')} | "
            f"Confidence: {log.get('confidence')}% | "
            f"Concern: {log.get('concern', 'None')}"
        )

    war_text = "\n".join(war_lines) if war_lines else "No war room logs today."

    return f"""
TODAY'S DATE: {datetime.now().strftime("%Y-%m-%d")}

PERFORMANCE SUMMARY:
  Total Trades:   {stats.get('total_trades', 0)}
  Winning Trades: {stats.get('winning_trades', 0)}
  Losing Trades:  {stats.get('losing_trades', 0)}
  Gross P&L:      ₹{stats.get('gross_pnl', 0):.2f}

TRADE DETAILS:
{trades_text}

WAR ROOM DEBATE LOG:
{war_text}
""".strip()


# ════════════════════════════════════════════════════════════
#   PERSISTENCE
# ════════════════════════════════════════════════════════════

def _save_reflection(date: str, insights: dict):
    """Saves today's reflection to data/reflections/YYYY-MM-DD.json"""
    path = os.path.join(REFLECTIONS_DIR, f"{date}.json")
    with open(path, "w") as f:
        json.dump(insights, f, indent=2)
    logger.info(f"Reflection saved: {path}")


def _update_running_learnings(insights: dict):
    """
    Appends key learnings to a running file.
    Morning screener can read this for additional context.
    """
    learnings_path = "data/learnings.json"

    existing = []
    if os.path.exists(learnings_path):
        with open(learnings_path, "r") as f:
            existing = json.load(f)

    # Keep last 10 days only
    existing.append({
        "date":          insights.get("date"),
        "grade":         insights.get("overall_grade"),
        "summary":       insights.get("one_line_summary"),
        "adjustments":   insights.get("tomorrow_adjustments", []),
    })
    existing = existing[-10:]

    with open(learnings_path, "w") as f:
        json.dump(existing, f, indent=2)


def _load_war_room_log_today() -> list:
    """Reads today's war room entries from DB."""
    import sqlite3
    from datetime import date

    db_path = "data/alcosoft.db"
    if not os.path.exists(db_path):
        return []

    today = date.today().isoformat()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM war_room_log
            WHERE timestamp LIKE ?
            ORDER BY id ASC
        """, (f"{today}%",)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"War room log read failed: {e}")
        return []


# ── Fallbacks ─────────────────────────────────────────────────
def _empty_reflection(date: str) -> dict:
    return {
        "date":                    date,
        "win_rate_pct":            0,
        "patterns_observed":       ["No trading activity today"],
        "what_worked":             "N/A",
        "what_failed":             "N/A",
        "tomorrow_adjustments":    ["Resume normal operation tomorrow"],
        "confidence_calibration":  "N/A",
        "overall_grade":           "N/A",
        "one_line_summary":        "No trades executed today.",
    }


def _fallback_reflection(date: str, stats: dict) -> dict:
    total   = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    return {
        "date":                    date,
        "win_rate_pct":            win_pct,
        "patterns_observed":       ["Owl Alpha unavailable — raw stats only"],
        "what_worked":             "Check trade log manually",
        "what_failed":             "Check trade log manually",
        "tomorrow_adjustments":    ["No AI adjustments — Owl API failed"],
        "confidence_calibration":  "Unknown",
        "overall_grade":           "B" if win_pct >= 50 else "C",
        "one_line_summary":        f"{win_pct}% win rate today.",
    }


if __name__ == "__main__":
    run_reflection_loop()