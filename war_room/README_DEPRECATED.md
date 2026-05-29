# ⚰️ War Room — Deprecated Component

**Status**: Archived (code preserved, not executed)

**Deprecation Date**: 2026-05-28

---

## What Was War Room?

War Room was a **multi-agent debate system** that analyzed stocks and made AI-driven trading decisions.

### Architecture (Original)
```
Top 4 Screener Stocks
    ↓
    ├─ Technical AI (analyzes charts)
    ├─ Fundamental AI (analyzes earnings)
    ├─ Risk AI (analyzes volatility)
    └─ Mediator AI (summarizes & decides)
        ↓
    BUY / REJECT Decision
```

### How It Worked
1. Ran every ~30 minutes during market hours (9 AM - 3 PM)
2. Analyzed top 4 stocks selected by morning screener
3. Each AI specialist examined the stock from different angles
4. Agents "debated" through multiple rounds
5. Mediator AI synthesized opinions into final decision
6. Final decision: APPROVE (BUY_ONLY) or REJECT (no trades)

---

## Why It Was Deprecated

### 1. **No Deterministic Validation**
- AI outputs were probabilistic and persuasion-driven
- Impossible to prove if War Room *improved* or *reduced* profitability
- Different models/prompts gave different answers

### 2. **Only Blocked Trades, Never Created Upside**
```
Strategy says: BUY      Outcome: Stock ↑
War Room says: REJECT   Result: Profit Lost
```
- War Room never *approved* a trade the strategy would have rejected
- It only *filtered* — so success rate needed to be extremely high to justify overhead

### 3. **Huge Operational Complexity**
- Multiple API keys, OpenRouter accounts
- Debate scheduling, context passing, parsing
- Agent coordination, logging, error handling
- All this infrastructure for one decision every 30 minutes

### 4. **Real Example: COALINDIA**
- Strategy: BUY (high volume, strong signals)
- War Room: REJECT (broad market looked bearish)
- Actual outcome: Stock moved UP
- Lesson: War Room's holistic views led to missed opportunities

---

## Strategic Shift: From Debate to Cognition

Instead of replacing War Room with another AI system, the architecture evolved:

### Old (War Room Era)
```
Multiple opinions
Same moment
→ One decision
```

### New (Cognition Era)
```
Same intelligence
Across time
→ Continuous learning
```

**Key insight**: AI should not predict BUY/SELL. Instead:

1. **Strategy Engine** — Remains deterministic and mathematical (EMA, RSI, patterns)
2. **Cognition Loop** — Continuously observes market, validates patterns, tracks behavior
3. **Reflection Layer** — Analyzes outcomes, calculates win rates, detects regime changes
4. **Adaptation** — Strategy parameters auto-tune based on measured performance

---

## How to Re-Enable War Room (if needed)

⚠️ **Warning**: Re-enabling requires understanding that War Room adds complexity but may not add value.

### Step 1: Restore Scheduler Job
In `main.py` (line ~262):
```python
# Uncomment these lines:
from core.trading_settings import get as cfg
war_room_interval = int(cfg("scheduling", "war_room_interval_minutes", 30))
scheduler.add_job(
    run_war_room,
    trigger="interval",
    minutes=war_room_interval,
    id="war_room",
    name="AlcoSoft War Room",
    max_instances=1,
)
```

### Step 2: Restore Import
In `main.py` (line ~89):
```python
from war_room.orchestrator import run_war_room
```

### Step 3: Enable Gating
In `core/strategy.py` (line ~75):
```python
WAR_ROOM_GATING = True
```

### Step 4: Update Startup Message
In `main.py` (scheduler logging section):
```python
f"   War room          : Every {war_room_interval} minutes\n"
```

---

## Code Organization

- `orchestrator.py` — Main entry point (run_war_room function)
- `agents/` — Individual AI specialists:
  - `technical.py` — Chart/technical analysis
  - `fundamental.py` — Earnings/P&E analysis
  - `risk.py` — Volatility/drawdown analysis
  - `mediator.py` — Consensus decision logic
  - `base_agent.py` — Shared base class
- `prompts/` — LLM prompts for each agent

---

## Data References

- **Input**: Top 4 stocks from morning screener (in briefing.approved_stocks)
- **Output**: Decision stored in `war_room_log` table
- **Logging**: `audit_war_room_decision()` in audit_logger.py
- **Circuit breaker**: "war_room" entry in circuit_breaker.py (tracks failures)

---

## Related Components (Still Active)

- **Reflection Loop** (run_reflection_loop) — Analyzes actual trade outcomes, calculates win rates
- **Morning Screener** (run_morning_screener) — Selects initial 25 stocks, filters to top 4 for war room
- **Strategy Engine** (run_strategy_loop) — Mathematical trading logic, pattern detection
- **Circuit Breaker** — Safety mechanisms for all components

---

## Future Direction

The system is evolving toward:

1. **Continuous Cognition Loop** — Same AI examining market across 15-min intervals
2. **Statistical Validation** — Measure signal reliability with hard numbers
3. **Adaptive Parameters** — Auto-tune SL width, position size, entry filters based on measured performance
4. **Local LLM** — Move from cloud API calls to local quantized models (cheaper, faster for iterative loops)

This is more sustainable and doesn't require constant human re-prompting.

---

## Questions?

Refer to the main project context document for the full architectural vision.

Last updated: 2026-05-28
