# AlcoSoft — Fix Progress (paused)

Working clone: `/app/AlcoSoft_Financial_Services` (all changes are local, NOT pushed to GitHub).
All 14 modified files compile (`python3 -m py_compile` passes). Nothing was committed/pushed.

## ✅ DONE (26 of 30 issues)

### Critical (8/8)
- **P2-1** broker_reconciliation.py — SL/target no longer inverted: pass real direction
  ("LONG"), fixed `calculate_target(entry, direction)` arg, recovered trades set `action`.
- **L2** order_executor.py — `_squareoff_done` now resets on date rollover (`_squareoff_done_date`).
- **R1** kotak_client.force_reconnect() now calls new `data_fetcher.restart_live_feed()` to rebind
  the WebSocket onto the fresh session.
- **S1** health_monitor.continuous_monitoring() now ACTS: `_act_on_health()` reconnects broker,
  restarts feed, and fires throttled `alert_critical` (was log-only).
- **P2-2** engine top-of-loop emergency-squareoff handler (works when market closed/looping) +
  engine heartbeat file; dashboard falls back to in-process squareoff if heartbeat stale.
- **L1** order_executor.py:~2164 — removed bogus `max_candles=30` kwarg (RSI exit now runs).
- **L4** order_executor.py — clear `kotak_sl_order_id` in DB right after SL cancel so recovery
  re-arms if the SELL later times out (no more naked-but-"protected" position).
- **P3-Q3** auth_manager.py — LOCAL_ADMIN_BYPASS now requires `LOCAL_ADMIN_BYPASS_TOKEN` via
  `X-Local-Admin-Token` header (no longer passwordless) + brute-force lockout on login.

### High (4/4)
- **L3** check_max_daily_loss() now includes unrealized P&L.
- **E2** BUY timeout re-queries broker status; REJECTED/CANCELLED not saved; unknown flagged
  reconciliation-pending.
- **E1** short squareoff fallback uses entry×1.05 (cover-buy above market), long keeps ×0.95.
- **P3-Q1** adaptive symbol-SL multiplier clamped to [0.75,1.25] when risk-based sizing is OFF.

### Medium/Low done
- **E3** regime_filter fails CLOSED (NO REGIME) on data error, not BULL.
- **E4** strategy indicator seed uses `include_current=False` (no repainting).
- **P2-3** emergency_squareoff preserves real SUCCESS/PARTIAL/FAILED status.
- **P2-4** dashboard settings POST guards risk-critical changes while positions open (409 unless
  `confirm_risky_change=true`).
- **P2-5** trading_settings alerts on corrupt config (was silent DEFAULTS fallback).
- **P2-6** cognition_engine builds real NIFTY trend (regime + yfinance), removed hardcoded BULLISH.
- **P2-7 / P3-6** busy_timeout=30 on dashboard + reflection_engine + cognition_engine sqlite conns.
- **P2-8** dashboard persists Flask secret (data/.flask_secret) — no more logout-on-restart.
- **P3-5** adaptive writes routed through `trading_settings.save_settings` under a cross-process
  fcntl lock; both writers now serialize.
- **P3-9** reflection.db / alcosoft.db / config path all anchored to project root.
- **P2-9** MULTIPLIER_FLOOR 0.4 → 0.6.  **P3-3** get_signal_execution_policy min_trades 10 → 20.
- **P3-Q4** _calculate_trade_time_decay returns 0.0 (discard) on corrupt timestamp, not 1.0.
- **L5** partial-profit & RSI exits persist their DONE guard BEFORE selling (no repeat sells).

## ⛔ REMAINING (4 issues — TODO on resume)
- **P3-1** (Medium) broker_reconciliation.py: positions closed via
  `mark_position_reconciled_closed` still don't call `reflection_engine.record_trade` →
  learning stats blind to reconciled/broker closes. FIX: after the `local_only` close branch
  (~line 475) call record_trade with the position's entry/exit/pnl.
- **R2/R3** (Medium) core/data_fetcher.py: guard `_token_to_symbol` writes in
  `resolve_instrument_tokens` (~line 641) and shared-client mutation with `_lock`.
- **P3-Q2-latent** (Medium) reflection/cognitive_agents.py:~442 delete dead
  `cognitive_signal_evaluation()` (uncapped 10% boost).
- **P3-4** (Low) reflection/cognitive_agents.py:~243 remove misleading duplicate-key
  `AGENT_SCHEDULE` dict (unused).

## To push when ready
Use the chat's **"Save to GitHub"** feature (do not git push manually).
