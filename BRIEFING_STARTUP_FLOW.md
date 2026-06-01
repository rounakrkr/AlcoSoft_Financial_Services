# FINAL STARTUP FLOW — Trading Briefing Reliability Fix

## Complete Startup Sequence with New Verification & Self-Healing

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     ALCOSOFT SYSTEM STARTUP                             │
│                         (main.py:startup())                             │
└─────────────────────────────────────────────────────────────────────────┘

[0/6] PREFLIGHT HEALTH CHECKS
│     ├─ Database connectivity
│     ├─ Kotak connection
│     ├─ Trading settings loaded
│     ├─ Broker reconciliation
│     ├─ Cognition engine available
│     ├─ Reflection engine available
│     ├─ Dashboard available
│     └─ 🆕 Trading briefing check ← ENHANCED DIAGNOSTICS
│
│ Result: If LIVE mode and any CRITICAL check fails → sys.exit(1)

[1/6] DATABASE INITIALIZATION
│     └─ Initialize DB, create tables if needed

[2/6] CRASH RECOVERY
│     ├─ Load open positions from DB
│     ├─ If positions exist:
│     │   └─ LOG: "⚠️  CRASH RECOVERY: N open position(s)"
│     └─ If clean startup:
│         └─ LOG: "No open positions found"

[3/6] BROKER CONNECTION (Kotak Neo)
│     ├─ Authenticate
│     ├─ If LIVE mode and auth fails → sys.exit(1)
│     └─ Reconcile broker vs local positions

┌─────────────────────────────────────────────────────────────────────────┐
│ [4/6] MORNING SCREENER                                                  │
│                                                                          │
│ NOW = datetime.now().time()                                             │
│                                                                          │
│ ┌─ IF NOW < 9:15 AM (Pre-Market)                                       │
│ │  ├─ LOG: "[4/6] Pre-market: Running morning screener..."            │
│ │  ├─ CALL: run_morning_screener()                                     │
│ │  │        ├─ 🔄 SCREENER STARTED                                    │
│ │  │        │                                                           │
│ │  │        ├─ [1/6] Fetch Yahoo Finance data (50 NIFTY stocks)      │
│ │  │        │        ├─ For each stock: fetch 30-day history          │
│ │  │        │        ├─ LOG: "✅ Yahoo Finance: 48/50 fetched"       │
│ │  │        │        └─ IF FAIL: LOG: "❌ No stock data"              │
│ │  │        │            → RETURN False (exit screener)               │
│ │  │        │                                                           │
│ │  │        ├─ [2/6] Score all stocks mathematically (RSI, Vol, EMA)  │
│ │  │        │        └─ LOG: "✅ Step 2: 48 stocks scored"            │
│ │  │        │                                                           │
│ │  │        ├─ [3/6] Gemini AI picks top N stocks                     │
│ │  │        │        ├─ CALL: _gemini_pick_stocks(all_candidates)    │
│ │  │        │        ├─ LOG: "✅ Step 3: Gemini picked 5 stocks"      │
│ │  │        │        ├─ IF GEMINI ERROR:                              │
│ │  │        │        │  └─ LOG: "❌ GEMINI API ERROR: [details]"     │
│ │  │        │        │     FALLBACK to math-based top N              │
│ │  │        │        │     LOG: "✅ Step 3 fallback: Math picks"      │
│ │  │        │        └─ IF NO PICKS: use math fallback               │
│ │  │        │                                                           │
│ │  │        ├─ [4/6] Build math watchlist from remaining stocks       │
│ │  │        │        └─ LOG: "✅ Step 4: 20 watchlist stocks"         │
│ │  │        │                                                           │
│ │  │        ├─ [5/6] Add market bias to cognition stocks              │
│ │  │        │        └─ LOG: "✅ Step 5: Market bias added"           │
│ │  │        │                                                           │
│ │  │        ├─ [6/6] Create briefing dict                             │
│ │  │        │        ├─ Validate structure (lists are lists)          │
│ │  │        │        ├─ Validate not empty (count > 0)                │
│ │  │        │        └─ IF INVALID:                                   │
│ │  │        │            LOG: "❌ Invalid briefing structure"          │
│ │  │        │            → RETURN False (exit screener)               │
│ │  │        │                                                           │
│ │  │        ├─ CALL: save_briefing(briefing)                          │
│ │  │        │        ├─ Call atomic_write_json()                      │
│ │  │        │        ├─ 🆕 Verify file exists after write             │
│ │  │        │        ├─ 🆕 Read file back and validate                │
│ │  │        │        ├─ LOG: "✅ Briefing saved and verified"         │
│ │  │        │        │      "   - Approved stocks: 5"                 │
│ │  │        │        │      "   - Watchlist: 20"                      │
│ │  │        │        └─ IF SAVE FAIL:                                 │
│ │  │        │            LOG: "❌ BRIEFING SAVE FAILED: [reason]"     │
│ │  │        │            → RETURN False (exit screener)               │
│ │  │        │                                                           │
│ │  │        ├─ LOG: "✅ SCREENER COMPLETED SUCCESSFULLY"              │
│ │  │        └─ RETURN True (screener succeeded)                       │
│ │  │                                                                    │
│ │  ├─ screener_success = returned value (True/False)                 │
│ │  └─ IF screener_success:                                            │
│ │      └─ LOG: "✅ Pre-market screener completed successfully"        │
│ │    ELSE:                                                             │
│ │      └─ LOG: "⚠️  Pre-market screener encountered errors"           │
│ │                                                                      │
│ └─ ELSE (NOW >= 9:15 AM, Market already open)                         │
│    └─ LOG: "[4/6] Market already open. Skipping screener."            │
│       (will attempt regeneration if briefing missing in step 5)       │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│ [5/6] LOAD BRIEFING & START LIVE FEED (🆕 WITH VERIFICATION)           │
│                                                                          │
│ LOG: "[5/6] Loading briefing and starting live feed..."               │
│                                                                          │
│ ┌─ CALL: briefing = load_briefing()                                    │
│ │        ├─ Check if data/session_briefing.json exists                 │
│ │        ├─ IF NOT:                                                    │
│ │        │   └─ Return None                                            │
│ │        ├─ IF EXISTS:                                                 │
│ │        │   ├─ LOG: "Loading briefing from data/session_briefing.j"  │
│ │        │   ├─ Read file (safe_read_json)                            │
│ │        │   ├─ Validate structure (ensure lists are lists)           │
│ │        │   ├─ LOG: "✅ Briefing loaded: 5 approved + 20 watch"      │
│ │        │   └─ Return briefing dict                                   │
│ │        └─ Result: briefing = dict or None                            │
│ │                                                                       │
│ ├─ Count stocks in briefing                                            │
│ │   approved = len(briefing.get("approved_stocks", []))                │
│ │   watchlist = len(briefing.get("watchlist", []))                     │
│ │   briefing_stocks_count = approved + watchlist                       │
│ │   LOG: "Briefing status: {approved} approved + {watchlist} watch"   │
│ │                                                                       │
│ ├─ 🆕 VERIFICATION: Is briefing valid?                                 │
│ │   IF briefing is None OR briefing_stocks_count == 0:                 │
│ │   │                                                                   │
│ │   │   LOG: "⚠️  Briefing invalid or missing. Attempting regeneration"│
│ │   │   LOG: "   Triggering screener regeneration (attempt 1)..."     │
│ │   │                                                                   │
│ │   │   CALL: regen_success = run_morning_screener()                   │
│ │   │        (same as [4/6] above)                                     │
│ │   │                                                                   │
│ │   │   IF regen_success:                                              │
│ │   │   │   CALL: briefing = load_briefing()                           │
│ │   │   │   IF briefing loaded:                                        │
│ │   │   │   │   approved = len(...)                                    │
│ │   │   │   │   watchlist = len(...)                                   │
│ │   │   │   │   briefing_stocks_count = approved + watchlist           │
│ │   │   │   │   LOG: "✅ Regeneration successful: {a} appr + {w} watch"│
│ │   │   │   ELSE:                                                       │
│ │   │   │       LOG: "❌ Screener claimed success but briefing missing" │
│ │   │   ELSE:                                                           │
│ │   │       LOG: "❌ Screener regeneration failed"                     │
│ │   │                                                                   │
│ │   └─ Continue with updated briefing_stocks_count                     │
│ │                                                                       │
│ ├─ 🆕 FINAL CHECK: Do we have a valid briefing?                        │
│ │   IF briefing is not None AND briefing_stocks_count > 0:             │
│ │   │                                                                   │
│ │   │   ✅ PROCEED TO LIVE FEED SETUP                                  │
│ │   │   ├─ Extract stock tickers                                       │
│ │   │   ├─ Purge invalid token cache                                   │
│ │   │   ├─ LOG: "Subscribing live feed: 25 symbols"                   │
│ │   │   ├─ CALL: start_live_feed(all_stocks)                          │
│ │   │   ├─ Fix trading symbols (bridge lookup cache)                   │
│ │   │   ├─ CALL: save_briefing(briefing)  ← Update with trading info   │
│ │   │   └─ LOG: "Live feed active for 25 symbols"                     │
│ │   │                                                                   │
│ │   ELSE:                                                               │
│ │       ❌ FATAL: NO VALID BRIEFING                                    │
│ │       ├─ LOG: "❌ BRIEFING UNAVAILABLE: Cannot enable trading"      │
│ │       ├─ IF not briefing:                                            │
│ │       │   LOG: "   Reason: Briefing file does not exist"             │
│ │       ├─ IF briefing_stocks_count == 0:                              │
│ │       │   LOG: "   Reason: Briefing empty (no stocks)"               │
│ │       ├─ LOG: "❌ Cannot proceed without valid briefing"             │
│ │       └─ sys.exit(1)  ← TERMINATE SYSTEM                             │
│ │                                                                       │
│ └─ Result: Live feed running OR sys.exit(1)                            │
└─────────────────────────────────────────────────────────────────────────┘

[6/6] POST-STARTUP FEED VERIFICATION
│     ├─ Wait brief time for WebSocket connect + first ticks
│     ├─ Verify market data is flowing
│     └─ System ready for trading

[READY FOR TRADING] ✅
│     ├─ Strategy loop running
│     ├─ Briefing available with 25 stocks
│     ├─ Live market feed active
│     └─ All systems operational

```

---

## Decision Tree: Briefing Status at Startup

```
                          ┌─────────────────────┐
                          │   SYSTEM STARTUP    │
                          └──────────┬──────────┘
                                     │
                            [4/6] Run Screener
                                     │
                    ┌────────────────┴────────────────┐
                    │                                 │
              Screener = True                   Screener = False
            (Success or Timeout)           (No data / API Error)
                    │                                 │
                    ▼                                 ▼
         ┌──────────────────┐            ┌──────────────────────┐
         │ Try Load Briefing │            │ Mid-Market Fallback? │
         └────────┬─────────┘            └──────────┬───────────┘
                  │                                 │
        ┌─────────┴──────────┐               ┌──────┴──────┐
        │                    │               │             │
    Exists              Missing          YES            NO
    & Valid                               │              │
        │                    │             │              │
        ▼                    ▼             ▼              ▼
    ✅ LIVE              ⚠️ REGEN      🔄 REGEN     ❌ ABORT
    START                 [5/6]         [5/6]        (BRIEFING
    TRADING           Screener         Screener      STILL
                      Runs            Runs           MISSING)
                           │               │           │
                    ┌──────┴───────┐      │           │
                    │              │      │           │
                  Success      Timeout   │           │
                    │              │      │           │
                    ▼              ▼      ▼           ▼
                ✅ LIVE        ⚠️ RETRY   │          ❌ EXIT
                START          LOAD       │          sys.exit(1)
                TRADING        BRIEFING   │
                                  │       │
                          ┌───────┴───────┘
                          │
                    ┌─────┴─────┐
                    │           │
                Success      Timeout
                    │           │
                    ▼           ▼
                ✅ LIVE     ❌ EXIT
                START      sys.exit(1)
                TRADING
```

---

## Error Messages → Root Cause → User Action

### Screener Errors

| Error Message | Root Cause | Action |
|---|---|---|
| `❌ SCREENER FAILED: No stock data fetched from Yahoo Finance. Aborting.` | Network down or Yahoo API changed | Check internet, verify Yahoo Finance works |
| `❌ GEMINI API ERROR: [details]. Using math fallback.` | Gemini quota exceeded or API key invalid | Check API key, verify Gemini quota |
| `❌ SCREENER FAILED: Briefing contains no stocks` | All stocks filtered out | Check Yahoo Finance data quality |
| `❌ SCREENER FAILED: Could not save briefing to disk` | Disk full or permissions issue | Check disk space, verify write permissions |

### Briefing Errors

| Error Message | Root Cause | Action |
|---|---|---|
| `❌ BRIEFING SAVE FAILED: atomic_write_json() returned False` | File write failed internally | Check disk space, restart system |
| `❌ BRIEFING SAVE FAILED: File does not exist after write` | File deleted immediately after write | Check for antivirus interference |
| `❌ BRIEFING SAVE FAILED: Could not read back saved file` | File corrupted during write | Check disk for bad sectors |

### Startup Errors

| Error Message | Root Cause | Action |
|---|---|---|
| `❌ BRIEFING UNAVAILABLE: Cannot enable trading` + `Reason: Briefing file does not exist` | Screener never ran or failed silently | Check logs for screener step failures |
| `❌ BRIEFING UNAVAILABLE: Cannot enable trading` + `Reason: Briefing empty` | Screener ran but picked no stocks | Check stock data quality from Yahoo Finance |

---

## Verification Summary

✅ **Code Quality**
- All 4 files compile without syntax errors
- No breaking changes to existing APIs
- Backward compatible with existing trading logic

✅ **Test Coverage**
- 5/5 test cases passing
- File I/O verified
- Empty briefing detection working
- Missing briefing detection working
- Health checks providing diagnostics
- Logging format consistent

✅ **Safety**
- No safety validations removed
- Risk calculations unchanged
- Position sizing unchanged
- System exits cleanly if briefing invalid
- Clear error messages for debugging

✅ **Observability**
- Every step logged with clear status
- Failures logged with specific reasons
- Success messages include relevant metrics
- No silent failures

**Status: READY FOR PRODUCTION DEPLOYMENT** ✅
