# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/cognitive_agents.py — Cognitive Observation Loop
#
#   4 LLM agents running serially every 15 minutes.
#   Each observes market patterns WITHOUT controlling execution.
# ============================================================

import json
import logging
import os
from datetime import datetime, time as dt_time
from typing import Optional

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
#   AGENT PROMPTS
# ════════════════════════════════════════════════════════════

def get_agent_system_prompt(agent_name: str) -> str:
    """Get system prompt for each agent."""
    
    base_prompt = """
You are an expert market researcher observing AlcoSoft's trading performance.

CRITICAL: You are RESEARCH ONLY. You do NOT:
- place trades
- reject trades
- disable signals
- modify execution
- change risk settings

You ARE:
- observing market patterns
- generating hypotheses
- critiquing previous predictions
- tracking prediction outcomes
- detecting anomalies
- studying regime transitions

Your output is RESEARCH DATA ONLY for later analysis.

Return ONLY valid JSON with NO explanations outside the JSON.
"""

    agent_prompts = {
        "A": base_prompt + """
AGENT A — Market Structure Observer
Focus on: overall market structure, trend direction, volatility regime, breadth

Analyze:
1. Current market state (bullish/bearish/ranging)
2. Volatility environment
3. Sector strength
4. Volume profile
5. Previous predictions accuracy

Generate observations about market structure evolution.
""",
        "B": base_prompt + """
AGENT B — Signal Performance Analyst
Focus on: signal reliability, confidence calibration, win rates by type

Analyze:
1. Which signals are working best today
2. Confidence vs actual performance mismatch
3. Time-of-day signal reliability
4. Symbol-specific patterns
5. Review Agent A's observations

Generate predictions about signal performance tomorrow.
""",
        "C": base_prompt + """
AGENT C — Regime Transition Specialist
Focus on: market regime changes, adaptive multiplier shifts, anomalies

Analyze:
1. Is the market regime changing?
2. Are adaptive multipliers tracking correctly?
3. Unusual patterns or anomalies
4. Risk environment shifts
5. Review Agents A & B observations

Generate hypotheses about regime behavior.
""",
        "D": base_prompt + """
AGENT D — Meta-Pattern Synthesizer
Focus on: integrating all observations, finding contradictions, evolving models

Analyze:
1. Consistency across agents A, B, C
2. Disagreements and why
3. Recurring themes
4. New patterns emerging
5. Unresolved questions from previous cycles

Generate meta-observations about the cognitive chain itself.
""",
    }

    return agent_prompts.get(agent_name, base_prompt)


def get_agent_context_prompt(
    agent_name: str,
    market_snapshot: dict,
    previous_cycles: list,
    unresolved_hypotheses: list,
    prediction_reviews: list
) -> str:
    """Build context for agent observation.
    
    SAFE FOR FIRST CYCLE:
    - Handles empty history (first trading day)
    - Gracefully supports missing DB state
    - No assumptions about prior observations
    """

    # Format previous agent observations (safe for empty list)
    prev_obs = ""
    if previous_cycles and len(previous_cycles) > 0:
        prev_obs = "\nPREVIOUS AGENT OBSERVATIONS:\n"
        for cycle in previous_cycles[-5:]:  # Last 5 cycles
            if cycle and hasattr(cycle, 'agent') and hasattr(cycle, 'market_observation'):
                prev_obs += f"\n[Agent {cycle.agent} @ {cycle.timestamp}]\n{cycle.market_observation}\n"
    else:
        prev_obs = "\n(No previous observations - first trading cycle)\n"

    # Format unresolved hypotheses (safe for empty list)
    hyp_text = ""
    if unresolved_hypotheses and len(unresolved_hypotheses) > 0:
        hyp_text = "\nUNRESOLVED HYPOTHESES:\n"
        for h in unresolved_hypotheses[-10:]:  # Last 10
            if isinstance(h, dict) and 'hypothesis' in h:
                hyp_text += f"  - {h['hypothesis']} (confidence: {h.get('confidence', 0):.1%})\n"
    else:
        hyp_text = "\n(No unresolved hypotheses yet)\n"

    # Format recent prediction reviews (safe for empty list)
    reviews_text = ""
    if prediction_reviews and len(prediction_reviews) > 0:
        reviews_text = "\nRECENT PREDICTION OUTCOMES:\n"
        success_count = 0
        for r in prediction_reviews[-5:]:  # Last 5
            if isinstance(r, dict):
                result = r.get('result', 'unknown')
                if result == 'success':
                    success_count += 1
                reviews_text += f"  - {result}: {r.get('analysis', '')}\n"
        if prediction_reviews:
            total = len([r for r in prediction_reviews if isinstance(r, dict)])
            reviews_text = f"\nPREDICTION ACCURACY: {success_count}/{total} recent predictions correct\n" + reviews_text
    else:
        reviews_text = "\n(No prediction outcomes yet)\n"

    context = f"""
CURRENT MARKET SNAPSHOT (as of {market_snapshot.get('timestamp')}):
  Trades today: {market_snapshot.get('total_trades_today', 0)} ({market_snapshot.get('win_rate', 0):.1f}% win rate)
  Winning trades: {market_snapshot.get('winning_trades', 0)}
  Gross P&L: ₹{market_snapshot.get('gross_pnl', 0):.2f}
  Active positions: {market_snapshot.get('active_positions', 0)}
  NIFTY Trend: {market_snapshot.get('nifty_trend', 'UNKNOWN')}
  
  Top signals: {', '.join(market_snapshot.get('top_signals', []) or ['None yet'])}
  Strong time windows: {len(market_snapshot.get('strong_time_windows', []))}
  Weak time windows: {len(market_snapshot.get('weak_time_windows', []))}

{hyp_text}

{prev_obs}

{reviews_text}

INSTRUCTIONS FOR AGENT {agent_name}:
1. Analyze the market snapshot and previous observations
2. Generate market observations (1-3 sentences)
3. Make 2-4 predictions with confidence scores
4. Review any previous predictions from this cycle
5. Identify regime notes, anomalies, and patterns
6. Ask questions for the next agent to explore
7. Return as valid JSON ONLY
"""

    return context


# ════════════════════════════════════════════════════════════
#   AGENT EXECUTION
# ════════════════════════════════════════════════════════════

def call_cognitive_agent(agent_name: str, context: str) -> Optional[dict]:
    """
    Call a cognitive agent via LLM abstraction layer.
    Supports both OpenRouter (cloud) and Ollama (local) with automatic fallback.
    
    Returns structured observation or None on failure (gracefully degraded).
    """
    try:
        from reflection.cognition_llm_client import generate_cognition_response
    except ImportError:
        logger.error("Cognition LLM client not available. Cannot call cognitive agent.")
        return None
    
    try:
        system = get_agent_system_prompt(agent_name)
        
        # Call LLM provider abstraction (handles OpenRouter + Ollama + fallback)
        result = generate_cognition_response(
            system_prompt=system,
            user_message=context,
            response_format="json"
        )
        
        if result and isinstance(result, dict):
            result["agent"] = agent_name
            result["timestamp"] = datetime.now().isoformat()
            logger.info(f"✅ Cognitive Agent {agent_name} observation received")
            return result
        else:
            logger.warning(f"❌ Agent {agent_name} returned no valid response")
            return None
    
    except Exception as e:
        logger.warning(f"❌ Cognitive Agent {agent_name} failed: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   AGENT ROTATION LOOP
# ════════════════════════════════════════════════════════════

# EXECUTION ENGINE stops taking trades at 3:00 PM
# But COGNITION continues observing until 3:15 PM (last market observation)
# FINAL REFLECTION happens at 3:35 PM (after market fully closed)

AGENT_ROTATION = ["A", "B", "C", "D"]
CYCLE_INTERVAL_MINUTES = 15
FIRST_CYCLE_TIME = dt_time(9, 30)   # 9:30 AM IST (after first 15-min candle)
LAST_COGNITION_TIME = dt_time(15, 15)  # 3:15 PM IST (final market observation)
EXECUTION_STOP_TIME = dt_time(15, 0)   # 3:00 PM IST (execution stops but cognition continues)

def should_run_cognitive_cycle() -> tuple[bool, Optional[str]]:
    """
    Check if current time matches cognitive cycle schedule.
    Returns (should_run, agent_name)
    
    IMPORTANT:
    - Execution stops at 3:00 PM
    - Cognition continues until 3:15 PM (last observation)
    - Market close is 3:30 PM
    - Final reflection at 3:35 PM (separate from cognition)
    """
    now = datetime.now().time()
    
    # Market opens at 9:15 AM IST, closes at 3:30 PM
    market_open = dt_time(9, 15)
    market_close = dt_time(15, 30)  # 3:30 PM
    
    # Cognition runs until 3:15 PM (AFTER execution stops at 3:00 PM)
    # This allows market observation through the final minutes
    if now < market_open or now > LAST_COGNITION_TIME:
        return False, None
    
    # Calculate which cycle we're in
    # First cycle at 9:30, then every 15 minutes
    current_minute = now.hour * 60 + now.minute
    first_minute = FIRST_CYCLE_TIME.hour * 60 + FIRST_CYCLE_TIME.minute
    
    if current_minute < first_minute:
        return False, None
    
    minutes_since_first = current_minute - first_minute
    
    # Check if we're at a cycle boundary (0, 15, 30, 45 minutes after first)
    if minutes_since_first % CYCLE_INTERVAL_MINUTES == 0:
        cycle_index = (minutes_since_first // CYCLE_INTERVAL_MINUTES) % len(AGENT_ROTATION)
        agent = AGENT_ROTATION[cycle_index]
        return True, agent
    
    return False, None


def run_cognitive_observation_cycle():
    """
    Execute one cognitive observation cycle.
    Called every 15 minutes during market hours (9:30 AM - 3:15 PM).
    
    MARKET OBSERVATION (not trade-tied):
    - Agents observe market patterns throughout the day
    - Continue observing even after execution stops at 3:00 PM
    - Last observation cycle at 3:15 PM captures end-of-day market behavior
    - This provides valuable data for next-day analysis
    
    SAFE FIRST-CYCLE INITIALIZATION:
    - No assumptions about previous state
    - Handles empty DB gracefully
    - First cycle uses empty lists safely
    """
    should_run, agent_name = should_run_cognitive_cycle()
    
    if not should_run or not agent_name:
        return
    
    now = datetime.now()
    logger.info(f"🧠 Starting cognitive observation cycle: Agent {agent_name} @ {now.isoformat()}")
    
    try:
        from reflection.cognition_engine import (
            build_market_snapshot,
            load_recent_cognition_cycles,
            get_unresolved_hypotheses,
            get_today_prediction_reviews,
            save_cognition_cycle,
            CognitionCycle,
        )
        
        # Build inputs for agent (safe for empty state)
        try:
            snapshot = build_market_snapshot()
        except Exception as e:
            logger.warning(f"Market snapshot failed, using minimal snapshot: {e}")
            snapshot = {
                "timestamp": now.isoformat(),
                "total_trades_today": 0,
                "winning_trades": 0,
                "win_rate": 0.0,
                "gross_pnl": 0.0,
                "active_positions": 0,
                "nifty_trend": "UNKNOWN",
                "top_signals": [],
                "strong_time_windows": [],
                "weak_time_windows": [],
            }
        
        # Load cognition history (safe for first cycle - may be empty)
        try:
            previous_cycles = load_recent_cognition_cycles(limit=20) or []
        except Exception as e:
            logger.warning(f"Could not load previous cycles: {e}")
            previous_cycles = []
        
        # Load hypotheses (safe for no hypotheses yet)
        try:
            hypotheses = get_unresolved_hypotheses() or []
        except Exception as e:
            logger.warning(f"Could not load hypotheses: {e}")
            hypotheses = []
        
        # Load reviews (safe for no reviews yet)
        try:
            reviews = get_today_prediction_reviews() or []
        except Exception as e:
            logger.warning(f"Could not load reviews: {e}")
            reviews = []
        
        # Calculate cycle number (for tracking within trading day)
        now_time = now.time()
        current_minute = now_time.hour * 60 + now_time.minute
        first_minute = FIRST_CYCLE_TIME.hour * 60 + FIRST_CYCLE_TIME.minute
        minutes_since_first = current_minute - first_minute
        cycle_num = max(1, (minutes_since_first // CYCLE_INTERVAL_MINUTES) + 1)
        
        # Build context
        context = get_agent_context_prompt(
            agent_name,
            snapshot,
            previous_cycles,
            hypotheses,
            reviews
        )
        
        # Call agent
        result = call_cognitive_agent(agent_name, context)
        
        if result:
            # Create cycle object
            cycle = CognitionCycle(
                datetime.now().isoformat(),
                agent_name,
                cycle_num
            )
            cycle.market_observation = result.get("market_observation", "")
            cycle.predictions = result.get("predictions", [])
            cycle.previous_prediction_review = result.get("previous_prediction_review", [])
            cycle.regime_notes = result.get("regime_notes", "")
            cycle.anomalies = result.get("anomalies", [])
            cycle.potential_patterns = result.get("potential_patterns", [])
            cycle.questions_for_future_agents = result.get("questions_for_future_agents", [])
            cycle.confidence_level = result.get("confidence_level", 0.5)
            
            # Save cycle
            save_cognition_cycle(cycle)
            
            logger.info(f"✅ Cognitive cycle complete: Agent {agent_name}")
        else:
            logger.warning(f"⚠️ Agent {agent_name} produced no output. Skipping save.")
    
    except Exception as e:
        logger.error(f"❌ Cognitive observation cycle failed: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════
#   STARTUP CHECK
# ════════════════════════════════════════════════════════════

def register_cognitive_cycle_scheduler():
    """
    Register the cognitive cycle scheduler with the main strategy loop.
    Should be called during system startup.
    
    The strategy loop (or a separate scheduler) should call run_cognitive_observation_cycle()
    periodically or on a cron schedule.
    """
    logger.info("🧠 Cognitive observation loop registered. Will run every 15 minutes during market hours.")
