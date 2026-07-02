# AlcoSoft Financial Services — Audit Fix Session

## Context
Algorithmic trading bot (Python; Kotak/NeoAPI broker). Cloned from
https://github.com/rounakrkr/AlcoSoft_Financial_Services (main branch).
Task: fix the final 4 of 30 audit issues (26 already fixed previously).

## Fixes completed (2026-06)
- **P3-1** `core/broker_reconciliation.py` (~L479): after `mark_position_reconciled_closed()`
  now calls `record_trade()` with entry/exit/computed-pnl so reconciled closes feed learning stats.
- **R2/R3** `core/data_fetcher.py`: `_token_to_symbol` write (L642), NeoAPI client + subscription
  mutations in `start_live_feed` (L768) and `stop_live_feed` (L860, L877) now guarded with `_lock`.
- **P3-Q2-latent** `reflection/cognitive_agents.py`: deleted dead `cognitive_signal_evaluation()` (uncapped 10% boost, no callers).
- **P3-4** `reflection/cognitive_agents.py`: removed misleading duplicate-key `AGENT_SCHEDULE` dict (unused).

Verified: all three files `py_compile` clean; grep confirms both dead symbols removed and lock guards present.

## Notes / Backlog
- **P3-7/P3-8** `reflection/llm_gateway.py`: FIXED (2026-06) — cognition no longer fails silently.
  Added `gateway_online()` + rate-limited `alert_gateway_offline()`, wired into all silent-failure paths.

## Live-engine capital bugs (2026-06) — FIXED
- **Bug 1 (nightly Capital API failure loop):** `_get_available_capital` hit the broker `limits()` API
  24/7 via the 5-min health monitor; off-hours empty payloads were misclassified as failures →
  escalation + destructive `force_reconnect()`. Fix: `_capital_fetch_window_open()` gate (trading-day
  08:45–15:30) serves cache off-market (no failure count); empty-payload defense; force_reconnect gated to window.
- **Bug 2 (capital_start collapses to free margin, e.g. ₹34.62):** `capital_start` was never persisted
  (startup at 2:32 AM off-market bypass; no market-open re-init), so `get_margin_status`/`check_max_daily_loss`
  fell back to raw broker free-margin. Fixes: (1) 9:14 AM scheduler lock-in + retry each observation cycle
  (`main.py`); (2) `initialize_daily_capital` LIVE only persists a FRESH capital reading (`is_capital_fresh()`);
  (3) `get_margin_status` reconstructs start-of-day capital = free_margin + open-position margin when
  capital_start is NULL; `check_max_daily_loss` reuses it.
- Tests: `tests/test_capital_bugfixes.py` (3 passing) — off-market gate, reconstruction (recovers ₹20,060), persisted-value respect.

## WS/capital regression (duplicate-connection storm) — FIXED (2026-06)
Root: capital path fired `force_reconnect()` (new sid invalidates WS) + `force_refresh` every scan +
negative `Net` misclassified as failure + unserialized multi-source reconnects.
- **Fix A** `order_executor._get_available_capital`: removed `force_reconnect()` from capital failure path (alert-only).
- **Fix B** `order_executor.get_margin_status`: reconstruction fallback uses cached `_get_available_capital()` (no force_refresh).
- **Fix C** `_get_available_capital`: numeric `Net` incl. ≤0 is a VALID free-cash reading (available=max(0,Net)),
  not a failure; guards `data:None`; `is_capital_fresh()` now uses `_capital_last_fetch_ok` flag (0 can be fresh).
- **Fix D** WebSocket serialization: `data_fetcher.start_live_feed` fully wrapped in the single reentrant
  `_reconnect_lock` (shared by timer reconnects + `restart_live_feed` + startup); `_client_epoch` stale-callback
  guard on `_on_open/_on_close/_on_error`; `kotak_client.force_reconnect` deduped+throttled (in-progress flag +
  30s min interval); single-timer cancel-before-schedule retained.
- Tests: `tests/test_ws_capital_hardening.py` (4 passing) — negative-Net valid, broken payload=failure,
  stale-epoch close ignored, force_reconnect throttle. (Installed `pyotp` in test env to import kotak_client.)
- Changes are LOCAL only; not pushed to GitHub (use "Save to Github").

## Bug hunt round 2 (2026-06, fresh clone) — 2 fixes DONE
- **Fix #1 (CRITICAL, TZ):** engine compared naive datetime.now() vs hardcoded IST times with no tz
  handling → off by 5.5h on a UTC host (no trading / no SL monitoring in real market hours). Enforced
  `os.environ["TZ"]="Asia/Kolkata"; time.tzset()` at startup in BOTH entrypoints (`main.py`, `dashboard/app.py`).
  Verified: naive now() now = IST, `is_market_session_open()` returns True during IST hours.
- **Fix #2 (partial-profit):** `order_executor.check_profit_targets` now skips positions tagged
  `PARTIAL_PROFIT_DONE`, so trailing SL manages the remainder (previously full-exited at same target,
  silently nullifying partial booking when fraction<1). Compiles clean.

### Open findings from hunt (NOT yet fixed — for discussion)
- MEDIUM: no in-repo user/admin provisioning — `generate_password_hash` imported but unused; dashboard
  login unusable without out-of-band DB seed or LOCAL_ADMIN_BYPASS.
- LOW: auth login timing-based username enumeration; per-IP brute-force key → global lockout DoS behind proxy;
  errorhandler leaks `str(error)` to client.
- Not yet reviewed: rest of reflection/ (adaptive_config_updater looks well-clamped) + screener/morning_screener.py.
