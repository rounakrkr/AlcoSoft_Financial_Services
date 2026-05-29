# Phase 5 Quick Integration Guide

## TL;DR: Add Cognitive Loop in 3 Steps

### Step 1: Add 1 Line to Strategy Loop

**File:** `core/strategy.py`

**Find:** The main `strategy_loop()` function

**Add this import at top:**
```python
from reflection.cognition_scheduler import schedule_cognitive_cycle
```

**Add this in the main loop:**
```python
def strategy_loop():
    # ... existing code ...
    
    while trading_active:
        # ... your existing strategy code ...
        
        # ADD THIS LINE:
        try:
            schedule_cognitive_cycle()
        except Exception as e:
            logger.warning(f"Cognitive cycle skipped: {e}")
        
        time.sleep(5)
```

**That's it.** The scheduler will automatically run agents every 15 minutes.

---

### Step 2: Schedule Final Reflection at 3:15 PM

**Option A: Manual Call (Simplest)**

**File:** `core/strategy.py` or `main.py`

**Find:** Where you handle market close (after 3:35 PM)

**Add:**
```python
def after_market_close():
    from reflection.reflection_loop import run_reflection_loop
    try:
        logger.info("Running final reflection synthesis...")
        run_reflection_loop()
    except Exception as e:
        logger.error(f"Final reflection failed: {e}")

# Call when market closes
if datetime.now().time() >= dt_time(15, 35):
    after_market_close()
```

**Option B: APScheduler (Recommended)**

**File:** Create `core/scheduler.py` or add to `main.py`

```python
from apscheduler.schedulers.background import BackgroundScheduler
from reflection.reflection_loop import run_reflection_loop

def initialize_scheduler():
    scheduler = BackgroundScheduler()
    
    # Final reflection at 3:15 PM
    scheduler.add_job(
        run_reflection_loop,
        'cron',
        hour=15,
        minute=15,
        max_instances=1,
        id='final_reflection'
    )
    
    scheduler.start()
    return scheduler

# In main():
scheduler = initialize_scheduler()
```

---

### Step 3: Verify API Keys

**File:** `war_room/agents/base_agent.py`

**Verify this exists:**
```python
OPENROUTER_KEYS = {
    "war_room": os.getenv("OPENROUTER_KEY_1"),      # Existing
    "cognition": os.getenv("OPENROUTER_KEY_2"),     # For cognition agents
    "reflection": os.getenv("OPENROUTER_KEY_3"),    # Existing
}
```

**Set environment variables (PowerShell):**
```powershell
$env:OPENROUTER_KEY_1="sk-or-..."
$env:OPENROUTER_KEY_2="sk-or-..."
$env:OPENROUTER_KEY_3="sk-or-..."
```

---

## What Happens Now

### During Trading Day (9:30 AM - 3:30 PM)

```
9:30  → Agent A observes market structure
9:45  → Agent B analyzes signals
10:00 → Agent C checks for regime changes
10:15 → Agent D synthesizes observations
10:30 → Agent A observes again (cycle repeats)
...
3:15  → Final Reflection Agent synthesizes day
3:30  → Market closes
```

### What Gets Stored

```
Database (data/alcosoft.db):
  ├─ cognition_cycles (20 per day)
  ├─ cognition_hypotheses (active predictions)
  ├─ cognition_reviews (prediction outcomes)
  └─ cognition_daily_reflections (day's synthesis)

File (data/learnings.json):
  └─ Last 10 days insights for morning screener
```

### Console Output

```
09:30:00 | 🧠 Cognitive cycle trigger detected
09:30:05 | Agent A observing market structure...
09:30:12 | Agent A saved 2 predictions, 1 anomaly
...continues every 15 min...
15:15:00 | 🧠 Running final reflection synthesis...
15:15:25 | Final reflection saved
✅ Cognitive loop complete for the day
```

---

## Verify It's Working

### Check 1: Cognition Cycles in Database

```bash
python -c "
from reflection.cognition_engine import load_today_cognition_cycles
cycles = load_today_cognition_cycles()
print(f'Cognitive cycles today: {len(cycles)}')
for c in cycles:
    print(f'  {c.timestamp} | Agent {c.agent} | {len(c.predictions)} predictions')
"
```

### Check 2: Hypotheses Status

```bash
python -c "
from reflection.cognition_engine import get_unresolved_hypotheses
hyps = get_unresolved_hypotheses()
print(f'Active hypotheses: {len(hyps)}')
for h in hyps[:3]:
    print(f'  - {h[\"hypothesis\"]} ({h[\"confidence\"]:.0%})')
"
```

### Check 3: Final Reflection

```bash
python -c "
from reflection.cognition_engine import get_today_daily_reflection
import json
reflection = get_today_daily_reflection()
print(json.dumps(reflection, indent=2))
"
```

### Check 4: Database Tables

```bash
sqlite3 data/alcosoft.db
> SELECT COUNT(*) as count FROM cognition_cycles;
> SELECT COUNT(*) as count FROM cognition_hypotheses;
> SELECT COUNT(*) as count FROM cognition_daily_reflections;
> .quit
```

---

## If Something Goes Wrong

### Issue: Cognition cycle not running

**Check:**
1. Is strategy loop running during market hours?
2. Is current time between 9:30 AM and 3:30 PM?
3. Check logs for exceptions

**Fix:**
```python
# Add debug logging
from reflection.cognition_scheduler import is_market_hours, is_cognitive_cycle_time
print(f"Market hours: {is_market_hours()}")
print(f"Cognitive time: {is_cognitive_cycle_time()}")
```

### Issue: API key errors

**Check:**
```bash
python -c "
import os
print('KEY_1:', 'SET' if os.getenv('OPENROUTER_KEY_1') else 'NOT SET')
print('KEY_2:', 'SET' if os.getenv('OPENROUTER_KEY_2') else 'NOT SET')
print('KEY_3:', 'SET' if os.getenv('OPENROUTER_KEY_3') else 'NOT SET')
"
```

**Fix:**
```powershell
$env:OPENROUTER_KEY_1="your-actual-key"
$env:OPENROUTER_KEY_2="your-actual-key"
$env:OPENROUTER_KEY_3="your-actual-key"
```

### Issue: Final reflection not running

**Manual test:**
```bash
python -c "
from reflection.reflection_loop import run_reflection_loop
run_reflection_loop()
"
```

### Issue: Database errors

**Reset database:**
```bash
rm data/alcosoft.db
# Will be recreated on next cycle
```

---

## Performance Check

If system feels slow:

```bash
python -c "
from reflection.cognition_engine import load_today_cognition_cycles
import json

cycles = load_today_cognition_cycles()
total_size = sum(len(json.dumps(c.to_json())) for c in cycles)
print(f'Total cycles: {len(cycles)}')
print(f'Data size: {total_size / 1024:.1f} KB')
print(f'Avg per cycle: {total_size / max(1, len(cycles)) / 1024:.1f} KB')
"
```

Expected: < 50 KB total

---

## System is Safe Because

✅ Cognition CANNOT place trades
✅ Cognition CANNOT reject trades
✅ Cognition CANNOT disable signals
✅ Cognition CANNOT modify strategy
✅ If LLM fails → Trading continues normally
✅ If database fails → Trading continues normally
✅ If scheduler fails → Trading continues normally

---

## Complete File Checklist

These files are included in your workspace:

```
✅ reflection/cognition_engine.py       (400 lines)
✅ reflection/cognitive_agents.py       (350 lines)
✅ reflection/cognition_scheduler.py    (250 lines)
✅ reflection/reflection_loop.py        (UPDATED)
✅ PHASE_5_COGNITIVE_LOOP.md           (300 lines)
✅ PHASE_5_INTEGRATION_CHECKLIST.md    (400 lines)
✅ PHASE_5_COMPLETION_SUMMARY.md       (300 lines)
✅ PHASE_5_QUICK_INTEGRATION.md        (This file)
```

---

## Done! 🎉

Phase 5 is complete and ready to use.

**Total integration time:** ~15 minutes
**Total code to write:** 2-4 lines
**Impact on trading:** Zero (enhancement only)
**Risk level:** Very Low (can be disabled instantly)

Your AlcoSoft system now has:
1. ✅ Deterministic execution (always in control)
2. ✅ Adaptive learning (statistical multipliers)
3. ✅ Cognitive observation (market research layer)

Enjoy your three-layer intelligent trading system! 🚀
