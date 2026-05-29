# PHASE 5 QUICK REFERENCE — DEPLOYMENT GUIDE

**Date:** 2026-05-29  
**Status:** READY FOR DEPLOYMENT ✅

---

## What's New in Phase 5 Updates

### 1. Timing Refinement
- **Before:** Cognition cycles ran until 3:30 PM
- **After:** Cognition cycles stop at 3:15 PM (before market close)
- **Result:** Final reflection runs at 3:35 PM with complete market data

### 2. Market-Tied Cognition
- Execution stops trading at 3:00 PM
- Cognition continues observing until 3:15 PM
- Captures closing market behavior for next-day analysis

### 3. Ollama Local LLM Support
- Cloud: OpenRouter (primary)
- Local: Ollama inference (fallback/on-premises)
- Auto-switching when either is unavailable

### 4. Cognition Lab Dashboard
- Separate `/cognition` portal (not on main dashboard)
- Keeps execution dashboard lightweight
- Provides agent observations, hypotheses, prediction accuracy

### 5. Integrated Scheduler
- Observation loop automatically triggers cognitive cycles
- No manual scheduling needed
- Runs every 15 minutes from 9:30 AM - 3:15 PM

---

## Pre-Deployment Checklist

- [ ] Run verification: `python PHASE_5_INTEGRATION_VERIFY.py`
- [ ] Check `.env` has OPENROUTER_KEY_2 (for cognition agents)
- [ ] Optional: Configure OLLAMA_BASE_URL if using local Ollama
- [ ] Verify database exists: `data/alcosoft.db`
- [ ] Check logs directory writable: `data/alcosoft.log`

---

## Files Changed

```
reflection/reflection_loop.py     (timing documentation)
reflection/cognition_scheduler.py (3:15 PM cutoff)
reflection/observation_loop.py    (scheduler integration)
dashboard/app.py                  (Cognition Lab blueprint)
```

**No breaking changes. All updates backward compatible.**

---

## Deployment Steps

### Step 1: Deploy Code
```bash
# Backup current version (optional)
git commit -am "Pre-Phase5 backup"

# Pull updates (or manually update the 4 files above)
git pull origin main

# Or if not using git:
cp PHASE_5_ARCHITECTURAL_UPDATES_COMPLETE.md docs/
```

### Step 2: Verify Installation
```bash
python PHASE_5_INTEGRATION_VERIFY.py

# Expected output: ✅ PHASE 5 VERIFICATION COMPLETE — ALL CHECKS PASSED
```

### Step 3: Test in Paper Mode
```bash
export TRADING_MODE=PAPER
python main.py

# Monitor for:
# - Cognition cycles every 15 min (9:30-3:15 PM)
# - No cycles after 3:15 PM
# - Reflection at 3:35 PM
```

### Step 4: Monitor Logs
```bash
# Terminal 1: Start system
python main.py

# Terminal 2: Monitor cognition cycles
tail -f data/alcosoft.log | grep -E "🧠|🦉|Owl"

# Expected timestamps:
# 09:30:XX 🧠 Cognitive cycle trigger detected
# 09:45:XX 🧠 Cognitive cycle trigger detected
# ...
# 15:15:XX 🧠 Cognitive cycle trigger detected
# 15:35:XX 🦉 Final Reflection Agent starting synthesis
```

### Step 5: Check Cognition Lab Dashboard
```bash
# In browser:
http://localhost:5000/cognition/status

# Should return JSON with:
# - cognition_cycles_today: ~20
# - active_hypotheses: varies
# - prediction_reviews: varies
# - llm_provider: "openrouter" or "ollama"
# - llm_available: true
```

---

## Environment Configuration

### Minimal (.env)
```bash
# Only required for cognition agents
OPENROUTER_KEY_2=sk-or-xxxxx
```

### Full (.env) - With Local Ollama
```bash
# Cognition LLM
COGNITION_LLM_PROVIDER=auto
OPENROUTER_KEY_2=sk-or-xxxxx
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral-small
```

### Provider Priority
- `auto`: Try OpenRouter first, fallback to Ollama
- `openrouter`: Cloud only
- `ollama`: Local only

---

## Timing Summary

| Time | Event | Status |
|------|-------|--------|
| 9:30 AM | Agent A observes | Cognition starts |
| 9:45 AM | Agent B analyzes signals | Continues |
| 10:00 AM | Agent C checks regime | Continues |
| 10:15 AM | Agent D synthesizes | Continues |
| ... | 15-min cycles continue | Every 15 min |
| 3:00 PM | Execution stops trading | ⚠️ Cognition **continues** |
| 3:15 PM | Agent B (last cycle) | **Cognition ends** |
| 3:30 PM | Market closes | Trading day ends |
| 3:35 PM | Final reflection | 🦉 Synthesis complete |

---

## Failure Modes (All Non-Critical)

### If OpenRouter API Key Missing
- OpenRouter unavailable
- Falls back to Ollama
- If both fail: cognition cycle skipped
- **Trading continues normally**

### If Ollama Not Running
- Ollama unavailable
- Falls back to OpenRouter
- If both fail: cognition cycle skipped
- **Trading continues normally**

### If LLM Timeout
- Cognition cycle logged as skipped
- No retry (prevents cascade)
- Next cycle tries again in 15 min
- **Trading continues normally**

### If Cognition Database Locked
- Warning logged
- Cycle deferred
- Next cycle retries
- **Trading continues normally**

---

## Success Indicators

After 15 minutes of running, should see:

✅ **Console Output**
```
09:30:05 | 🧠 Cognitive cycle trigger detected
09:30:12 | Agent A observing market structure...
09:30:15 | ✅ Cognitive Agent A observation received
09:45:05 | 🧠 Cognitive cycle trigger detected
09:45:12 | Agent B analyzing signal performance...
09:45:18 | ✅ Cognitive Agent B observation received
```

✅ **Database Records**
```sql
SELECT COUNT(*) FROM cognition_cycles; -- ~2-5 records
SELECT COUNT(*) FROM cognition_hypotheses; -- 0+ records
```

✅ **Daily Reflection (at 3:35 PM)**
```bash
ls data/reflections/
# Should show YYYY-MM-DD.json file for today
cat data/reflections/2026-05-29.json | python -m json.tool
```

✅ **Cognition Lab API**
```bash
curl http://localhost:5000/cognition/status
# { "cognition_cycles_today": ~20, "active_hypotheses": ..., ... }
```

---

## Rollback Procedure

If issues occur, quick rollback:

```bash
# Revert the 4 changed files
git checkout HEAD~1 reflection/reflection_loop.py
git checkout HEAD~1 reflection/cognition_scheduler.py
git checkout HEAD~1 reflection/observation_loop.py
git checkout HEAD~1 dashboard/app.py

# Restart
python main.py
```

Or manually revert the 4 file changes.

---

## Monitoring & Troubleshooting

### View Cognition Cycles
```bash
tail -f data/alcosoft.log | grep "🧠\|Agent.*observation"
```

### Check Reflection Completion
```bash
tail -f data/alcosoft.log | grep "Final Reflection\|Owl Alpha"
```

### Verify No Late Cognition Cycles
```bash
# Should have NO entries after 15:15:XX
grep "🧠 Cognitive" data/alcosoft.log | tail -5
```

### Check Cognition Database
```bash
sqlite3 data/alcosoft.db << EOF
SELECT strftime('%Y-%m-%d', timestamp) as date, agent, COUNT(*) as cycles
FROM cognition_cycles
GROUP BY date, agent
ORDER BY timestamp DESC
LIMIT 20;
EOF
```

---

## Testing Checklist (15 minutes)

- [ ] Cognition cycle at 9:30 AM (when market opens)
- [ ] Cycles continue every 15 minutes
- [ ] No cycles after 3:15 PM (if running then)
- [ ] Reflection completes by 3:40 PM (if live testing)
- [ ] `/cognition/status` returns HTTP 200
- [ ] No trading impact from any cognition failure
- [ ] `data/learnings.json` updates daily
- [ ] Hypotheses accumulate in database

---

## Next Steps (After Deployment)

1. **Monitor for 1 trading day** — confirm all systems working
2. **Review Cognition Lab endpoints** — ensure data quality
3. **Check learnings.json** — verify next-day screener reads it
4. **Enable live trading** (if confident) — TRADING_MODE=LIVE

---

## Support

| Issue | Solution |
|-------|----------|
| No cognition cycles | Check OPENROUTER_KEY_2 configured |
| No reflections | Verify scheduler in main.py (3:35 PM job) |
| Ollama not working | Start: `ollama serve`, Pull: `ollama pull mistral-small` |
| Dashboard 500 error | Check logs: `tail data/alcosoft.log` |
| Cycles after 3:15 PM | Verify cognition_scheduler.py `last_cycle = dt_time(15,15)` |

---

## Documentation

- **Full Details:** [PHASE_5_ARCHITECTURAL_UPDATES_COMPLETE.md](PHASE_5_ARCHITECTURAL_UPDATES_COMPLETE.md)
- **Original Design:** [PHASE_5_COGNITIVE_LOOP.md](PHASE_5_COGNITIVE_LOOP.md)
- **Integration:** [PHASE_5_INTEGRATION_CHECKLIST.md](PHASE_5_INTEGRATION_CHECKLIST.md)
- **Verification:** [PHASE_5_INTEGRATION_VERIFY.py](PHASE_5_INTEGRATION_VERIFY.py)

---

**Ready to deploy?** Start with `python PHASE_5_INTEGRATION_VERIFY.py` ✅

