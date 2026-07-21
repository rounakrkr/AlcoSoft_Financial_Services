# AlcoSoft Financial Services — PRD / Agent Memory

## Original Problem Statement
User is building an algo trading platform (repo: rounakrkr/AlcoSoft_Financial_Services) and wants to improve strategy profits. Data universe: Nifty Midcap 50 (user will provide data). Broker: Kotak Neo (free API). Data source for regime filter/screener: Upstox + yfinance. Wants BOTH AI-powered insights and pure quant improvements.

## Architecture (existing codebase — headless Python engine, NOT a web app)
- `main.py` — entry point: startup sequence, APScheduler jobs (08:45 screener, 15:15 squareoff, 15:30 EOD report, 15:35 reflection), asyncio strategy loop
- `core/` — strategy engine (strategy.py, 3200+ lines), order_executor, kotak_client, data_fetcher (WebSocket), regime_filter, state_manager (SQLite), health_monitor, broker_reconciliation
- `screener/morning_screener.py` — MIDCAP_50 universe screener
- `reflection/` — LLM cognition loop (agents A-D every 15 min), reflection engine, adaptive config updater (Groq/OpenAI via llm_gateway)
- `dashboard/` — Flask dashboard (login, settings, cognition lab)
- `research/` — extensive backtest suite (R7_COMB_486 = 272% return baseline), sweeps, blueprints
- `tests/` — pytest suite (4 files need live Kotak creds)
- Config: `config/trading_settings.json` (active: BUY_R7_VARIANT_D long, SHORT_STREAK_MOMENTUM_BREAKDOWN short, SELL_R7_EMA50_DYN_EXIT). Regime filter currently DISABLED in settings.
- Prior audit history in FIX_PROGRESS.md (30 issues, all now resolved in pushed code)

## What's Been Implemented (2026-06 session 1)
- Cloned repo to /app, installed all deps (neo_api_client v2.0.0 from Kotak-neo-api-v2 GitHub, ta, yfinance, groq, etc.), created missing `requirements.txt`
- **Bug audit + 4 fixes (all verified by testing agent, 27/27 tests pass):**
  1. CRITICAL: `reflection/cognitive_agents.py` — LAST_COGNITION_TIME/EXECUTION_STOP_TIME were dt_time(3,15)/dt_time(3,0) (3 AM not PM) → cognition loop NEVER ran during market hours. Fixed to 15:15/15:00.
  2. `core/strategy.py` — condition_macd_hist_rejection_bounce used 'macd_signal' column but indicators create 'macd_sig' → condition permanently dead. Fixed.
  3. `core/strategy.py` — condition_streak_close_0_above_period_max_10: duplicate definition removed + off-by-one window iloc[-12:-2] → iloc[-11:-1] (now includes previous candle, matches short mirror).
  4. `research/multi_timeframe_runner.py` — crashed on empty cached DataFrames (NaN vs Timestamp compare). Filter empty dfs. Also fixed deprecated fillna(method=).
- Updated stale `tests/test_fx06.py` to mock newer is_capital_fresh()/market-hours gates.

## Environment Notes
- Process TZ must be Asia/Kolkata (main.py enforces). Engine cannot fully run without Kotak creds (KOTAK_CONSUMER_KEY, KOTAK_MOBILE_NUMBER, KOTAK_UCC, KOTAK_MPIN, KOTAK_TOTP_SECRET in .env). LLM keys: GROQ/OpenAI via reflection/llm_gateway.
- Test command: `TZ=Asia/Kolkata python -m pytest tests/ --ignore=tests/test_f004_order_sync.py --ignore=tests/test_f005_capital_api.py --ignore=tests/test_production_engine.py --ignore=tests/test_sl_recovery_v2.py`

## Next Tasks / Backlog
- **P0: Profit-improvement analysis on Midcap50 data — WAITING FOR USER'S DATA.** Plan: run research/backtest suite on user data, analyze exit-reason distribution, sweep SL/PT/trailing params, evaluate regime filter ON vs OFF (currently disabled), position-sizing (risk-based sizing currently OFF), LLM-assisted trade autopsies.
- P1: Design suggestions given: centralise market-hour constants; split strategy.py into modules; make time functions TZ-aware (ZoneInfo) instead of process TZ.
- P2: Consider re-enabling regime filter after validating thresholds on the new data; risk-based position sizing evaluation.
