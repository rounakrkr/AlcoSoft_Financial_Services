# Phase 5 Architectural Update — Quick Deployment Guide

**Completed:** 2026-05-29
**All Changes:** ✅ 100% Complete
**Time to Deploy:** 15-20 minutes

---

## What Changed

### 1. ✅ LLM Provider Abstraction
**File:** `reflection/cognition_llm_client.py` (NEW - 600 lines)

Unified interface for:
- OpenRouter (cloud LLM)
- Ollama (local LLM)
- Automatic fallback

**Why:** Stop hardcoding API calls in agents. Support both cloud and local LLMs.

### 2. ✅ Safe First-Cycle Initialization
**File:** `reflection/cognitive_agents.py` (UPDATED)

Now handles:
- Empty database (first trading day)
- Missing previous observations
- No hypotheses yet
- No prediction reviews yet

**Why:** Agent A at 9:30 AM shouldn't crash with "NoneType" errors.

### 3. ✅ Extended Cognition Schedule
**File:** `reflection/cognitive_agents.py` (UPDATED)

New timing:
- **Last cognition cycle:** 3:15 PM (was stopping earlier)
- Continues after execution stops at 3:00 PM
- Captures end-of-day market behavior

**Why:** Final hour market patterns matter for next-day signals.

### 4. ✅ Moved Final Reflection
**File:** `reflection/reflection_loop.py` (UPDATED)

Timing changed:
- **Old:** 3:15 PM
- **New:** 3:35 PM
- **Reason:** Market fully closes at 3:30 PM; reflection needs complete data

### 5. ✅ Separate Cognition Dashboard
**File:** `dashboard/cognition_lab.py` (NEW - 600 lines)

Keeps main dashboard execution-focused:
- `/cognition/` — Research dashboard (separate)
- Shows agent observations
- Tracks hypotheses
- Displays prediction accuracy
- Shows anomalies & patterns
- Shows daily reflections

**Why:** Main dashboard stays lightweight; cognition data doesn't clutter execution view.

### 6. ✅ Updated Scheduler
**Files:** `reflection/cognition_scheduler.py`, `reflection/reflection_loop.py` (UPDATED)

New schedule:
```
3:00 PM → Execution stops
3:15 PM → Last cognition observation
3:30 PM → Market closes
3:35 PM → Final reflection runs
```

---

## 🚀 Deployment Steps (15 minutes)

### Step 1: Add LLM Environment Variables

Choose your approach:

**Option A: Cloud LLM (OpenRouter)**
```powershell
$env:COGNITION_LLM_PROVIDER="openrouter"
$env:OPENROUTER_KEY_2="sk-or-YOUR-KEY"
```

**Option B: Local LLM (Ollama)**
```powershell
# First: Start Ollama in another terminal
ollama serve

# Then set environment
$env:COGNITION_LLM_PROVIDER="ollama"
$env:OLLAMA_BASE_URL="http://localhost:11434"
$env:OLLAMA_MODEL="mistral-small"
```

**Option C: Auto-Fallback (Recommended)**
```powershell
$env:COGNITION_LLM_PROVIDER="auto"
$env:OPENROUTER_KEY_2="sk-or-YOUR-KEY"
$env:OLLAMA_BASE_URL="http://localhost:11434"
```

### Step 2: Update Dashboard

**File:** `dashboard/app.py`

Find where you initialize Flask:
```python
app = Flask(__name__)
```

Add after initialization:
```python
from dashboard.cognition_lab import init_cognition_lab

# Initialize Cognition Lab dashboard
init_cognition_lab(app)
```

**That's it.** Cognition dashboard now available at `http://localhost:5000/cognition/`

### Step 3: Update Scheduler (if using APScheduler)

**File:** `main.py` or `core/scheduling.py`

Find where you schedule final reflection:
```python
scheduler.add_job(
    run_reflection_loop,
    'cron',
    hour=15,
    minute=15,  # ← CHANGE THIS
    id='final_reflection'
)
```

Change to:
```python
scheduler.add_job(
    run_reflection_loop,
    'cron',
    hour=15,
    minute=35,  # ← CHANGED from 15 to 35
    id='final_reflection'
)
```

### Step 4: Test Integration

```bash
# Test LLM provider
python -c "
from reflection.cognition_llm_client import test_llm_providers
test_llm_providers()
"

# Should see:
# 🧠 LLM Provider Status:
#    Preferred: auto
#    OpenRouter: ✅ Available
#    Ollama: ❌ Unavailable
#    Available: ['openrouter']
```

### Step 5: Start Trading

```bash
python main.py
```

**What you'll see:**
```
09:30:00 | 🧠 Starting cognitive observation cycle: Agent A
09:30:12 | ✅ Cognitive cycle complete: Agent A
09:45:00 | 🧠 Starting cognitive observation cycle: Agent B
...continues every 15 minutes...
15:15:00 | 🧠 Starting cognitive observation cycle: Agent B
15:35:00 | 🦉 Final Reflection Agent starting synthesis
15:35:25 | ✅ Final reflection complete
```

---

## 🌐 Access Your Dashboards

### Main Dashboard (Execution)
```
http://localhost:5000/
```
- Orders
- Positions
- Risk metrics
- Trade execution

### Cognition Lab (Research)
```
http://localhost:5000/cognition/
```
- Agent observations
- Hypotheses
- Prediction tracking
- Anomalies
- Patterns
- Daily reflections

### API Endpoints (JSON)

```bash
# System status
curl http://localhost:5000/cognition/status

# Today's cycles
curl http://localhost:5000/cognition/cycles/today

# Hypotheses
curl http://localhost:5000/cognition/hypotheses

# Prediction accuracy
curl http://localhost:5000/cognition/predictions/accuracy

# Daily reflection
curl http://localhost:5000/cognition/reflection/today

# LLM provider status
curl http://localhost:5000/cognition/llm-status
```

---

## 📋 Verification Checklist

After deployment, verify:

- [ ] Cognition dashboard loads at `/cognition/`
- [ ] LLM provider shows as available
- [ ] Cognition cycles appear at 9:30, 9:45, 10:00, 10:15...
- [ ] Last cycle runs at 3:15 PM
- [ ] Final reflection runs at 3:35 PM
- [ ] No "NoneType" errors in logs
- [ ] Trading continues normally
- [ ] Dashboard shows agent observations
- [ ] Hypotheses tracking works
- [ ] Prediction accuracy calculated

---

## 🛠️ Troubleshooting

### Problem: "Cognition_llm_client not found"

**Fix:** Make sure file exists:
```bash
ls reflection/cognition_llm_client.py
```

If not, recreate it from documentation.

### Problem: "LLM provider unavailable"

**Check OpenRouter:**
```powershell
$env:OPENROUTER_KEY_2  # Should show your API key
```

**Check Ollama:**
```bash
ollama serve  # In another terminal
```

**Or use auto-fallback:**
```powershell
$env:COGNITION_LLM_PROVIDER="auto"
```

### Problem: "Dashboard shows no data"

**Make sure:**
1. Cognition cycles are running (check logs)
2. Dashboard is importing cognition_lab
3. Navigate to `/cognition/` (not `/`)
4. Check browser console for errors

### Problem: "First cycle crashes"

**Should NOT happen** with safe first-cycle initialization.

If it does:
1. Check logs for full error
2. Enable debug logging:
   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   ```
3. Check that `get_unresolved_hypotheses()` returns `[]` not `None`

---

## 📚 Documentation

Read these in order:

1. **PHASE_5_UPDATE_ARCHITECTURE_REFINEMENTS.md** (you are here)
   - What changed and why
   - Configuration examples
   - Integration steps

2. **PHASE_5_INTEGRATION_CHECKLIST.md**
   - Detailed deployment checklist
   - Testing procedures
   - Monitoring queries

3. **PHASE_5_QUICK_INTEGRATION.md**
   - TL;DR version
   - Minimal integration
   - First day expectations

---

## ⏰ Timeline

```
9:15 AM   │ Market opens
9:30 AM   │ Agent A starts observing (after 1st 15-min candle)
9:45 AM   │ Agent B analyzes
10:00 AM  │ Agent C checks regime
10:15 AM  │ Agent D synthesizes
...       │ Repeats every 15 minutes
3:00 PM   │ ← Execution STOPS (but cognition continues)
3:15 PM   │ ← LAST cognition cycle (Agent B)
3:30 PM   │ ← Market officially closes
3:35 PM   │ ← FINAL REFLECTION synthesizes day
```

---

## 🎯 Key Principles

✅ **Execution** always in control (deterministic)
✅ **Learning** evidence-based (adaptive multipliers)
✅ **Cognition** research-only (never controls trades)
✅ **Fallback** graceful (LLM fails → cognition skips, trading continues)
✅ **Dashboard** separated (main: execution, `/cognition/`: research)

---

## 💡 Example Usage

### Check System Status

```bash
curl -s http://localhost:5000/cognition/status | jq .
```

Output:
```json
{
  "status": "active",
  "timestamp": "2026-05-29T14:22:03.123456",
  "cognition_cycles_today": 8,
  "active_hypotheses": 5,
  "prediction_reviews": 12,
  "prediction_accuracy": "10/12",
  "llm_provider": "openrouter",
  "llm_available": true
}
```

### Check Prediction Accuracy

```bash
curl -s http://localhost:5000/cognition/predictions/accuracy | jq .
```

Output:
```json
{
  "total_predictions": 12,
  "successful": 10,
  "failed": 2,
  "unknown": 0,
  "accuracy_percent": 83.3
}
```

### Get Today's Reflection

```bash
curl -s http://localhost:5000/cognition/reflection/today | jq .
```

Output:
```json
{
  "date": "2026-05-29",
  "summary": "Market showing strong bullish structure with elevated volatility...",
  "strongest_patterns": [
    "Morning momentum continues through late session",
    "10:00-10:30 window shows consistent signal reliability"
  ],
  "failed_assumptions": [
    "Risk environment stable - late session showed spike"
  ],
  "regime_behavior": "Early bullish trend maintained through close",
  "confidence": 0.78
}
```

---

## ✨ What's Next

Phase 6 possibilities (future):

- [ ] Sentiment analysis integration
- [ ] News correlation tracking
- [ ] Advanced pattern mining
- [ ] ML-based confidence calibration
- [ ] Multi-market regime detection
- [ ] Agent consensus scoring
- [ ] Prediction ensemble voting

But first: **Deploy Phase 5 and validate it in production.**

---

## 📞 Support

Issues?

1. Check logs first
2. Run status check:
   ```bash
   python -c "
   from reflection.cognition_llm_client import get_llm_status
   import json
   print(json.dumps(get_llm_status(), indent=2))
   "
   ```
3. Check documentation
4. Review error messages carefully

---

**Status:** ✅ Ready to deploy
**Confidence:** Very High
**Risk:** Very Low (graceful fallback on all failures)

**Deploy Now!** 🚀
