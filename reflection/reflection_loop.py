# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/reflection_loop.py — Final Reflection Agent
#
#   Runs at 3:35 PM daily after market fully closes.
#   Synthesizes day's cognitive observation chain (Agents A/B/C/D).
#   Compares predictions vs reality, generates evolving market memory.
#
#   IMPORTANT: This is RESEARCH ONLY. Does NOT control execution.
#
#   TIMING SCHEDULE:
#   - 9:30 AM  → Agent A starts (market structure observer)
#   - 9:45 AM  → Agent B starts (signal performance analyst)
#   - 10:00 AM → Agent C starts (regime transition specialist)
#   - 10:15 AM → Agent D starts (meta-pattern synthesizer)
#   - ... continues every 15 minutes ...
#   - 3:00 PM  → Execution stops taking trades
#   - 3:15 PM  → Last cognition observation cycle (final agent run)
#   - 3:30 PM  → Market officially closes (no more trading)
#   - 3:35 PM  → Final Reflection Agent runs (after all data complete)
# ============================================================

import json
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

from core.state_manager import get_today_stats, get_recent_trades
from reflection.adaptive_config_updater import apply_adaptive_config

load_dotenv()
logger = logging.getLogger(__name__)

REFLECTIONS_DIR = "data/reflections"
os.makedirs(REFLECTIONS_DIR, exist_ok=True)


# ════════════════════════════════════════════════════════════
#   FINAL REFLECTION AGENT — END OF DAY
# ════════════════════════════════════════════════════════════

def run_reflection_loop():
    """
    Called at 3:35 PM daily (after market fully closes at 3:30 PM).
    
    Synthesizes the day's cognitive observation chain (Agents A/B/C/D).
    Compares agent predictions vs actual trade outcomes.
    Generates evolving market memory for next-day analysis.
    
    TIMING:
    - 3:00 PM: Execution stops taking trades
    - 3:15 PM: Last cognition observation cycle runs
    - 3:30 PM: Market officially closes
    - 3:35 PM: Final Reflection Agent runs (all data complete)
    
    CRITICAL: This is RESEARCH AND LEARNING ONLY.
    Does NOT modify strategy code or disable execution.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"🦉 Final Reflection Agent starting synthesis for {today}...")

    try:
        from reflection.cognition_engine import (
            load_today_cognition_cycles,
            get_unresolved_hypotheses,
            get_today_prediction_reviews,
            save_daily_cognition_reflection,
            compress_cognition_memory,
        )
    except ImportError:
        logger.error("Cognition engine not available. Falling back to legacy reflection.")
        _run_legacy_reflection(today)
        return

    # ── Gather today's data ────────────────────────────────────
    stats = get_today_stats()
    trades = get_recent_trades(days=1)
    
    # Load cognition chain from today
    cognition_cycles = load_today_cognition_cycles()
    hypotheses = get_unresolved_hypotheses()
    prediction_reviews = get_today_prediction_reviews()

    # Nothing to reflect on
    if stats["total_trades"] == 0 and not cognition_cycles:
        logger.info("No trades or cognitive observations today. Skipping final reflection.")
        _save_empty_reflection(today)
        return

    # ── Build context for final synthesis ──────────────────────
    context = _build_final_reflection_context(
        stats, trades, cognition_cycles, hypotheses, prediction_reviews
    )

    # ── Call Owl Alpha for synthesis ───────────────────────────
    insights = _call_owl_final(context)

    if not insights:
        logger.warning("Owl Alpha synthesis failed. Saving fallback reflection.")
        insights = _fallback_final_reflection(today, stats, cognition_cycles)

    insights["date"] = today
    insights["raw_stats"] = stats
    insights["cognition_cycle_count"] = len(cognition_cycles)

    # ── Save final reflection ─────────────────────────────────
    _save_reflection(today, insights)
    _update_running_learnings(insights)
    
    # ── Save to cognition storage ─────────────────────────────
    try:
        save_daily_cognition_reflection(insights)
    except Exception as e:
        logger.warning(f"Failed to save daily cognition reflection: {e}")

    # ── Compress old cognition ────────────────────────────────
    try:
        compress_cognition_memory(keep_cycles=20)
    except Exception as e:
        logger.warning(f"Cognition memory compression failed: {e}")

    logger.info(
        f"✅ Final reflection complete. "
        f"Win rate: {stats.get('winning_trades', 0)}/"
        f"{stats.get('total_trades', 0)} trades. "
        f"Cognition cycles: {len(cognition_cycles)}."
    )
    
    # ── Update Adaptive Configuration ──────────────────────────
    try:
        logger.info("🔧 Updating adaptive configuration based on daily reflection...")
        apply_adaptive_config()
        logger.info("✅ Adaptive configuration updated successfully")
    except Exception as e:
        logger.warning(f"Adaptive config update failed (non-critical): {e}")



# ════════════════════════════════════════════════════════════
#   OWL ALPHA FINAL SYNTHESIS
# ════════════════════════════════════════════════════════════

def _call_owl_final(context: str) -> dict | None:
    """
    Calls Final Reflection Agent via OpenRouter.
    OWL ALPHA is intentionally cloud-based for highest-quality synthesis.
    Runs at 3:35 PM after market close, does NOT affect trading.
    """
    try:
        from war_room.agents.base_agent import (
            _call_openrouter, _parse_json, OPENROUTER_KEYS, MODELS
        )
    except ImportError:
        logger.error("War room base agent not available. Cannot run final reflection.")
        return None

    key = OPENROUTER_KEYS.get("reflection")

    try:
        raw = _call_openrouter(
            key=key,
            model=MODELS["reflection"],
            system=_system_prompt_final(),
            user=context,
        )
        logger.info("✅ OWL Alpha response received")
        return _parse_json(raw)

    except Exception as e:
        logger.warning(f"OWL Alpha failed: {e}. Attempting GPT-OSS standby...")
        try:
            raw = _call_openrouter(
                key=key,
                model=MODELS["standby"],
                system=_system_prompt_final(),
                user=context,
            )
            logger.info("✅ GPT-OSS standby response received")
            return _parse_json(raw)
        except Exception as e2:
            logger.error(f"OWL Alpha and standby both failed: {e2}")
            return None


def _system_prompt_final() -> str:
    """System prompt for final reflection synthesis."""
    return """
Owl Alpha — Final Reflection Agent for AlcoSoft.
Synthesize the day's cognitive observation chain (Agents A/B/C/D).

CRITICAL: This is RESEARCH ONLY. Do NOT suggest code changes or execution modifications.

Your role:
1. Compare agent predictions vs actual trade outcomes
2. Identify strongest validated observations
3. Identify failed assumptions
4. Find recurring market behaviors
5. Note unusual anomalies
6. Generate next-day watch themes
7. Pose unresolved questions

JSON format:
{
  "cognition_summary": "one paragraph integrating all agent observations",
  "strongest_patterns": ["pattern 1", "pattern 2", "pattern 3"],
  "failed_assumptions": ["assumption that didn't hold", ...],
  "regime_behavior": "description of market regime",
  "unexpected_anomalies": ["anomaly 1", "anomaly 2"],
  "next_day_watch_themes": ["watch theme 1", "watch theme 2"],
  "unresolved_questions": ["question 1", "question 2"],
  "confidence_level": 0.75,
  "meta_observations": "observations about the observation process itself"
}
""".strip()


def _call_owl(context: str) -> dict | None:
    """Legacy call for compatibility."""
    return _call_owl_final(context)


def _system_prompt() -> str:
    """Legacy system prompt for compatibility."""
    return _system_prompt_final()



# ════════════════════════════════════════════════════════════
#   CONTEXT BUILDERS
# ════════════════════════════════════════════════════════════

def _build_final_reflection_context(
    stats: dict,
    trades: list,
    cognition_cycles: list,
    hypotheses: list,
    prediction_reviews: list
) -> str:
    """Builds context for final reflection synthesis."""

    # Trade summary
    trade_lines = []
    for t in trades:
        outcome = "WIN" if (t.get("pnl") or 0) > 0 else "LOSS"
        trade_lines.append(
            f"  {t['symbol']} | {t['status']} | Entry: ₹{t['entry_price']} | "
            f"Exit: ₹{t.get('exit_price', 'OPEN')} | P&L: ₹{t.get('pnl', 0):.2f} | {outcome}"
        )

    trades_text = "\n".join(trade_lines) if trade_lines else "No trades today."

    # Cognition summary
    cognition_text = "\nCOGNITION OBSERVATION CHAIN:\n"
    for cycle in cognition_cycles:
        cognition_text += (
            f"\n[Agent {cycle.agent} @ {cycle.timestamp}]\n"
            f"  Observation: {cycle.market_observation}\n"
        )
        if cycle.predictions:
            cognition_text += f"  Predictions: {len(cycle.predictions)} made\n"
        if cycle.anomalies:
            cognition_text += f"  Anomalies: {', '.join(cycle.anomalies)}\n"

    # Hypothesis status
    hypothesis_text = "\nHYPOTHESIS STATUS:\n"
    if hypotheses:
        for h in hypotheses[:5]:  # Top 5
            hypothesis_text += f"  - {h['hypothesis']} (confidence: {h.get('confidence', 0):.0%})\n"
    else:
        hypothesis_text += "  No active hypotheses.\n"

    # Prediction reviews
    reviews_text = "\nPREDICTION OUTCOMES:\n"
    if prediction_reviews:
        success_count = sum(1 for r in prediction_reviews if r.get('result') == 'success')
        total_reviews = len(prediction_reviews)
        reviews_text += f"  Reviewed: {total_reviews} predictions\n"
        reviews_text += f"  Successful: {success_count} ({success_count*100//max(1,total_reviews)}%)\n"
    else:
        reviews_text += "  No prediction reviews yet.\n"

    return f"""
TODAY'S DATE: {datetime.now().strftime("%Y-%m-%d")}

PERFORMANCE SUMMARY:
  Total Trades:   {stats.get('total_trades', 0)}
  Winning Trades: {stats.get('winning_trades', 0)}
  Losing Trades:  {stats.get('losing_trades', 0)}
  Gross P&L:      ₹{stats.get('gross_pnl', 0):.2f}

TRADE DETAILS:
{trades_text}

{cognition_text}

{hypothesis_text}

{reviews_text}

TASK:
1. Synthesize the day's cognitive observations
2. Compare agent predictions against actual trade outcomes
3. Identify strongest validated patterns
4. Identify failed assumptions
5. Describe market regime
6. Note anomalies
7. Generate next-day watch themes
8. Pose unresolved questions

Return ONLY valid JSON.
""".strip()


def _build_reflection_context(
    stats: dict,
    trades: list,
    war_room_log: list
) -> str:
    """Legacy context builder for compatibility."""
    
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
            try:
                existing = json.load(f)
            except:
                existing = []

    # Keep last 10 days only
    existing.append({
        "date":          insights.get("date"),
        "grade":         insights.get("overall_grade", "N/A"),
        "summary":       insights.get("one_line_summary", insights.get("cognition_summary", "N/A")),
        "patterns":      insights.get("strongest_patterns", []),
        "watch_themes":  insights.get("next_day_watch_themes", []),
    })
    existing = existing[-10:]

    with open(learnings_path, "w") as f:
        json.dump(existing, f, indent=2)


def _load_war_room_log_today() -> list:
    """Reads today's war room entries from DB (legacy support)."""
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


# ────────────────────────────────────────────────────────────
#   FALLBACK REFLECTIONS
# ────────────────────────────────────────────────────────────

def _save_empty_reflection(date: str):
    """Save when no trading or cognitive activity."""
    _save_reflection(date, {
        "date": date,
        "cognition_summary": "No trading or cognitive observations today",
        "strongest_patterns": [],
        "failed_assumptions": [],
        "regime_behavior": "N/A",
        "unexpected_anomalies": [],
        "next_day_watch_themes": ["Resume normal operation"],
        "unresolved_questions": [],
        "confidence_level": 0.0,
    })


def _fallback_final_reflection(date: str, stats: dict, cognition_cycles: list) -> dict:
    """Fallback when LLM synthesis fails."""
    total   = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    agent_count = len(set(c.agent for c in cognition_cycles))

    return {
        "date": date,
        "cognition_summary": f"Executed {total} trades with {win_pct}% win rate. {agent_count} agents observed patterns.",
        "strongest_patterns": ["Owl Alpha synthesis unavailable — patterns from data only"],
        "failed_assumptions": [],
        "regime_behavior": "Unable to determine — LLM synthesis failed",
        "unexpected_anomalies": [],
        "next_day_watch_themes": ["Monitor trading closely"],
        "unresolved_questions": ["What caused the LLM synthesis failure?"],
        "confidence_level": 0.3,
        "raw_stats": stats,
        "cognition_cycle_count": len(cognition_cycles),
    }


def _empty_reflection(date: str) -> dict:
    """Legacy empty reflection."""
    return {
        "date": date,
        "win_rate_pct": 0,
        "patterns_observed": ["No trading activity today"],
        "what_worked": "N/A",
        "what_failed": "N/A",
        "tomorrow_adjustments": ["Resume normal operation tomorrow"],
        "confidence_calibration": "N/A",
        "overall_grade": "N/A",
        "one_line_summary": "No trades executed today.",
    }


def _fallback_reflection(date: str, stats: dict) -> dict:
    """Legacy fallback reflection."""
    total   = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    return {
        "date": date,
        "win_rate_pct": win_pct,
        "patterns_observed": ["Owl Alpha unavailable — raw stats only"],
        "what_worked": "Check trade log manually",
        "what_failed": "Check trade log manually",
        "tomorrow_adjustments": ["No AI adjustments — Owl API failed"],
        "confidence_calibration": "Unknown",
        "overall_grade": "B" if win_pct >= 50 else "C",
        "one_line_summary": f"{win_pct}% win rate today.",
    }


def _run_legacy_reflection(date: str):
    """Run legacy reflection when cognition engine not available."""
    logger.warning("Running legacy reflection (no cognition engine)")
    stats = get_today_stats()
    trades = get_recent_trades(days=1)
    war_room_log = _load_war_room_log_today()

    if stats["total_trades"] == 0 and not war_room_log:
        _save_reflection(date, _empty_reflection(date))
        return

    context = _build_reflection_context(stats, trades, war_room_log)
    insights = _call_owl(context)

    if not insights:
        insights = _fallback_reflection(date, stats)

    insights["date"] = date
    insights["raw_stats"] = stats

    _save_reflection(date, insights)
    _update_running_learnings(insights)

    try:
        apply_adaptive_config()
    except Exception as e:
        logger.warning(f"Adaptive config update failed: {e}")


if __name__ == "__main__":
    run_reflection_loop()