# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   main.py — System Entry Point
#   Run this file. Everything starts from here.
#   python main.py
# ============================================================

import asyncio
import logging
import signal
import sys
import os
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import colorlog

load_dotenv()

# ─────────────────────────────────────────────────────────────
# FIX 1: UNICODE / EMOJI LOGGING ON WINDOWS
#
# ROOT CAUSE: Windows terminal (cmd/PowerShell) uses CP1252
# encoding by default. Python's FileHandler also uses the
# system default. Emojis (like ✅ 📦 🔍) are outside CP1252
# so every log.info("✅ ...") crashes with UnicodeEncodeError.
#
# THE FIX:
#   StreamHandler → force stdout to UTF-8 via reconfigure()
#   FileHandler   → explicitly pass encoding="utf-8"
#
# This is a one-time fix in setup_logging(). After this,
# emojis work in both terminal output AND alcosoft.log.
# ─────────────────────────────────────────────────────────────

def setup_logging():
    os.makedirs("data", exist_ok=True)
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # ── Force Windows terminal to accept UTF-8 emojis ────────
    # reconfigure() is Python 3.7+. On non-Windows this is a no-op.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ── Colored terminal handler (UTF-8 forced) ───────────────
    stream_handler = colorlog.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))

    # ── File handler with explicit UTF-8 encoding ─────────────
    # Without encoding="utf-8", Python uses the system default
    # (CP1252 on Windows) and crashes on any emoji character.
    file_handler = logging.FileHandler(
        "data/alcosoft.log",
        encoding="utf-8"    # ← THE CRITICAL FIX
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    logging.basicConfig(
        level=getattr(logging, log_level),
        handlers=[stream_handler, file_handler],
        force=True   # Override any handlers set before this call
    )

setup_logging()
logger = logging.getLogger("AlcoSoft")

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)


# ── Imports (after logging setup) ─────────────────────────────
from core.kotak_client import get_client, logout
from core.state_manager import initialize_db, recover_state, load_briefing, save_briefing, validate_briefing, is_briefing_safe_for_trading
from core.data_fetcher import start_live_feed, stop_live_feed
from core.strategy import run_strategy_loop
from screener.morning_screener import run_morning_screener
from reflection.reflection_loop import run_reflection_loop
from reflection.observation_loop import observation_loop_main


# ── Time Constants ────────────────────────────────────────────
SCREENER_TIME   = dt_time(8, 45)
MARKET_OPEN     = dt_time(9, 15)
MARKET_CLOSE    = dt_time(15, 30)
REFLECTION_TIME = dt_time(15, 35)


def _briefing_generated_date(briefing: dict | None):
    if not isinstance(briefing, dict):
        return None

    generated_at = briefing.get("generated_at")
    if not generated_at:
        return None

    text = str(generated_at).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _today_morning_screener_status(briefing: dict | None) -> tuple[bool, str]:
    is_valid, validity_reason = validate_briefing(briefing)
    if not is_valid:
        return False, f"briefing invalid: {validity_reason}"

    session_type = briefing.get("session_type")
    if session_type != "MORNING_SCREENER":
        return False, f"session_type is {session_type!r}, expected 'MORNING_SCREENER'"

    generated_date = _briefing_generated_date(briefing)
    today = datetime.now().date()
    if generated_date is None:
        return False, "generated_at is missing or invalid"
    if generated_date != today:
        return False, f"briefing date is {generated_date.isoformat()}, expected {today.isoformat()}"

    approved = len(briefing.get("approved_stocks", []))
    watchlist = len(briefing.get("watchlist", []))
    return True, (
        f"today's MORNING_SCREENER briefing exists "
        f"({approved} approved + {watchlist} watchlist)"
    )

# ─────────────────────────────────────────────────────────────
# FIX 2: PROPER ASYNC SHUTDOWN
#
# ROOT CAUSE: The old _shutdown() called loop.stop() directly.
# But asyncio.run(main()) is still waiting on
# "await run_strategy_loop()". Stopping the loop underneath
# a pending coroutine raises:
#   RuntimeError: Event loop stopped before Future completed.
#
# THE FIX: Use an asyncio.Event() as a cooperative shutdown
# flag. Instead of stopping the loop forcefully, we set the
# event. run_strategy_loop() checks this event and exits
# cleanly. Then main() completes naturally.
#
# _shutdown_event is set in the signal handler (sync context).
# run_strategy_loop() awaits it via asyncio.Event.wait().
# ─────────────────────────────────────────────────────────────
_shutdown_event: asyncio.Event = None


# ════════════════════════════════════════════════════════════
#   STARTUP SEQUENCE
# ════════════════════════════════════════════════════════════

async def startup():
    logger.info("=" * 55)
    logger.info("  ALCOSOFT FINANCIAL SERVICES — SYSTEM STARTING")
    logger.info(f"  Mode: {os.getenv('TRADING_MODE', 'PAPER')} | "
                f"Strategy: {os.getenv('STRATEGY_TYPE', 'INTRADAY')}")
    logger.info("=" * 55)

    # STEP 0 — Database must exist before health checks touch state tables.
    logger.info("[0/6] Initializing database...")
    initialize_db()

    from core.state_manager import initialize_daily_capital
    logger.info("[0.5/6] Initializing daily capital tracking...")
    initialize_daily_capital()

    # STEP 1 — Preflight Checks (before any trading)
    logger.info("[1/6] Running preflight health checks...")
    from core.health_monitor import run_preflight_checks
    health = run_preflight_checks()
    
    if os.getenv('TRADING_MODE', 'PAPER') == 'LIVE' and not health.passed():
        failed_checks = [k for k, v in health.checks.items() if not v]
        if failed_checks == ["Market Hours"]:
            logger.warning("⚠️ Market is closed. Preflight check bypassed. Engine will wait for market to open.")
        else:
            logger.critical("❌ LIVE MODE DISABLED: Preflight checks failed")
            logger.critical("Fix issues above and restart the system.")
            sys.exit(1)
    
    if not health.passed():
        logger.warning("⚠️  Some checks failed but continuing (PAPER MODE)")

    # Step 2 — Crash Recovery
    logger.info("[2/6] Checking for previous state...")
    state = recover_state()

    if state["open_position_count"] > 0:
        logger.warning(
            f"⚠️  CRASH RECOVERY: {state['open_position_count']} open position(s) "
            f"from last session. Monitoring resumed."
        )

    # Step 3 — Kotak Login
    logger.info("[3/6] Connecting to Kotak Neo...")
    try:
        client = await asyncio.to_thread(get_client)
        logger.info("✅ Kotak session established successfully.")
    except Exception as e:
        logger.critical(f"❌ Kotak login failed: {e}")
        logger.critical("Cannot proceed without broker connection. Exiting.")
        sys.exit(1)

    # Step 3b — Broker ↔ DB reconciliation (LIVE)
    logger.info("[3b/6] Reconciling broker vs local positions...")
    try:
        from core.broker_reconciliation import reconcile_broker_vs_local
        await asyncio.to_thread(reconcile_broker_vs_local)
    except Exception as e:
        logger.error(f"Broker reconciliation error: {e}")

    # Step 3c — Validate capital allocation configuration
    logger.info("[3c/6] Validating capital allocation configuration...")
    try:
        from core.order_executor import validate_allocation_config
        warnings = validate_allocation_config()
        if warnings:
            for warning in warnings:
                logger.warning(f"  {warning}")
        else:
            logger.info("✅ Capital allocation configuration valid")
    except Exception as e:
        logger.error(f"Capital allocation validation error: {e}")

    # Step 4 — Morning Screener
    # Must run on every startup unless today's valid MORNING_SCREENER briefing
    # already exists in data/session_briefing.json.
    screener_success = False
    
    # VALIDATION GATE 1: Check if today's morning screener record exists.
    logger.info("[4/6] Checking today's morning screener briefing...")
    briefing_status = load_briefing()
    has_today_screener, screener_skip_reason = _today_morning_screener_status(briefing_status)
    
    # RUN SCREENER IF today's valid MORNING_SCREENER briefing is absent.
    should_run_screener = not has_today_screener
    
    if should_run_screener:
        logger.warning(
            "[4/6] Running morning screener because %s.",
            screener_skip_reason,
        )
        try:
            screener_success = run_morning_screener()
            if screener_success:
                logger.info("[BRIEFING] Generated Screener Briefing ✅")
            else:
                logger.warning("[BRIEFING] Screener encountered errors")
        except Exception as e:
            logger.error(f"[BRIEFING] Screener exception: {e}")
            logger.warning("[BRIEFING] Will verify existing briefing or attempt regeneration.")
    else:
        logger.info("[4/6] Skipping screener: %s.", screener_skip_reason)

    # Step 5 — Load briefing and VALIDATE FOR TRADING
    logger.info("[5/6] Loading and validating briefing for trading...")
    briefing = load_briefing()
    
    # VALIDATION GATE 2: Comprehensive validation + current-date screener gate
    is_safe, safety_reason = is_briefing_safe_for_trading(briefing)
    if is_safe:
        has_today_screener, today_reason = _today_morning_screener_status(briefing)
        if not has_today_screener:
            is_safe = False
            safety_reason = f"Current-date morning screener briefing required: {today_reason}"
    
    # If validation failed, attempt regeneration ONCE
    if not is_safe:
        already_ran_screener = should_run_screener and screener_success
        if not already_ran_screener:
            logger.warning(f"[BRIEFING] Validation failed: {safety_reason}")
            logger.warning("[BRIEFING] Attempting screener regeneration (attempt 1)...")
            try:
                regen_success = run_morning_screener()
                if regen_success:
                    briefing = load_briefing()
                    is_safe, safety_reason = is_briefing_safe_for_trading(briefing)
                    if is_safe:
                        has_today_screener, today_reason = _today_morning_screener_status(briefing)
                        if not has_today_screener:
                            is_safe = False
                            safety_reason = (
                                "Current-date morning screener briefing required: "
                                f"{today_reason}"
                            )
                    if is_safe:
                        logger.info("[BRIEFING] Validated ✅")
                    else:
                        logger.error(f"[BRIEFING] Regeneration failed validation: {safety_reason}")
                else:
                    logger.error("[BRIEFING] Screener regeneration failed")
            except Exception as e:
                logger.error(f"[BRIEFING] Regeneration exception: {e}")
        else:
            logger.error(f"[BRIEFING] Validation failed: {safety_reason} (screener already ran)")
    else:
        logger.info("[BRIEFING] Validated ✅")

    # SAFETY CHECK: Only proceed if briefing is safe
    if not is_safe:
        logger.error("[BRIEFING] Rejected")
        logger.critical(f"Cannot proceed to trading. Reason: {safety_reason}")
        sys.exit(1)

    # Extract stock lists for feed subscription
    approved  = [s["ticker"] for s in briefing.get("approved_stocks", [])]
    watchlist = [s["ticker"] for s in briefing.get("watchlist", [])]
    all_stocks = list(dict.fromkeys(approved + watchlist))

    if all_stocks:
        from core.data_fetcher import purge_invalid_token_cache
        purge_invalid_token_cache()
        logger.info(
            f"[BRIEFING] Subscribing live feed: {len(all_stocks)} symbols "
            f"({len(approved)} cognition + {len(watchlist)} watchlist)"
        )
        start_live_feed(all_stocks)

        # Fix trading symbols in briefing
        from core.data_fetcher import fix_briefing_trading_symbols
        fix_briefing_trading_symbols(briefing)
        save_briefing(briefing)

        logger.info(f"[BRIEFING] Saved")
        logger.info(f"Live feed active for {len(all_stocks)} symbols.")
        if approved:
            logger.info(f"  Cognition picks : {approved}")
        logger.info(f"  Math watchlist : {watchlist}")
    else:
        logger.error("[BRIEFING] Rejected: No stocks available for trading")
        logger.critical("Cannot proceed to trading without stocks. Exiting.")
        sys.exit(1)

    # Step 6 — Post-startup feed check (brief wait for WS connect + first ticks)
    logger.info("[6/6] Verifying live feed...")
    await asyncio.sleep(3)
    from core.health_monitor import check_live_feed
    feed_ok, feed_msg = check_live_feed(strict=True)
    if feed_ok:
        logger.info(f"✅ Live feed: {feed_msg}")
    else:
        logger.warning(f"⚠️  Live feed: {feed_msg}")

    logger.info("✅ System startup complete. Trading ready.")
    return True


# ════════════════════════════════════════════════════════════
#   OBSERVATION CYCLE WRAPPER
# ════════════════════════════════════════════════════════════

async def run_observation_cycle():
    """
    Wrapper for scheduler to run observation cycle.
    Called every 15 minutes by scheduler.
    """
    try:
        from reflection.observation_loop import run_observation_cycle as observation_run
        await observation_run()
    except Exception as e:
        logger.error(f"Observation cycle error: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════
#   EMERGENCY SQUAREOFF (15:15 PM)
# ════════════════════════════════════════════════════════════

async def run_emergency_squareoff():
    """
    Scheduled job: Squareoff all positions at 15:15 PM
    Ensures no positions held after market close.
    
    PROOF OF EXECUTION:
      Positions Before: X (logged)
      Positions After: 0 (logged)
      If After = 0, squareoff WORKED.
    """
    try:
        logger.info("\n" + "="*60)
        logger.info("⏰ 15:15 PM - EMERGENCY SQUAREOFF")
        logger.info("="*60)
        
        from core.order_executor import squareoff_all_intraday
        from core.state_manager import get_open_positions, get_today_gross_pnl, validate_briefing
        
        # BEFORE STATE
        positions_before = get_open_positions() or []
        pnl_before = get_today_gross_pnl()
        num_before = len(positions_before)
        
        logger.info("")
        logger.info("BEFORE SQUAREOFF:")
        logger.info(f"  Positions Open: {num_before}")
        if num_before > 0:
            for i, pos in enumerate(positions_before, 1):
                logger.info(f"    {i}. {pos.get('symbol', 'UNKNOWN')} - {pos.get('quantity', 0)} qty")
        logger.info(f"  Daily P&L: ₹{pnl_before}")
        
        # EXECUTE SQUAREOFF
        logger.info("")
        logger.info("EXECUTING SQUAREOFF...")
        result = squareoff_all_intraday(reason="SCHEDULED_MARKET_CLOSE")
        
        # AFTER STATE
        import time
        time.sleep(1)  # Allow DB to update
        positions_after = get_open_positions() or []
        pnl_after = get_today_gross_pnl()
        num_after = len(positions_after)
        
        logger.info("")
        logger.info("AFTER SQUAREOFF:")
        logger.info(f"  Positions Open: {num_after}")
        if num_after > 0:
            for i, pos in enumerate(positions_after, 1):
                logger.info(f"    {i}. {pos.get('symbol', 'UNKNOWN')} - {pos.get('quantity', 0)} qty")
        logger.info(f"  Daily P&L: ₹{pnl_after}")
        
        # PROOF
        logger.info("")
        if num_after == 0:
            logger.info("✅ SQUAREOFF WORKED: All positions closed")
        else:
            logger.warning(f"⚠️  SQUAREOFF INCOMPLETE: {num_after} positions still open")
        
        logger.info("="*60)
        
    except Exception as e:
        logger.error(f"❌ Emergency squareoff FAILED: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════
#   END-OF-DAY REPORT (15:30 PM)
# ════════════════════════════════════════════════════════════

def _read_today_trade_audit() -> dict:
    """Read today's trade/order facts from SQLite for end-of-day reporting."""
    import os
    import sqlite3
    from core.state_manager import DB_PATH

    today = datetime.now().strftime("%Y-%m-%d")
    fallback = {
        "trade_rows": 0,
        "buy_rows": 0,
        "closed_rows": 0,
        "open_rows": 0,
        "broker_order_rows": 0,
        "sl_order_rows": 0,
        "open_positions_missing_sl": 0,
    }
    if not os.path.exists(DB_PATH):
        return fallback

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS trade_rows,
                    SUM(CASE WHEN action = 'BUY' THEN 1 ELSE 0 END) AS buy_rows,
                    SUM(CASE WHEN status IN ('CLOSED', 'STOPPED') THEN 1 ELSE 0 END) AS closed_rows,
                    SUM(CASE WHEN status = 'OPEN' AND quantity > 0 THEN 1 ELSE 0 END) AS open_rows,
                    SUM(CASE WHEN COALESCE(kotak_order_id, '') <> '' THEN 1 ELSE 0 END) AS broker_order_rows,
                    SUM(CASE WHEN COALESCE(kotak_sl_order_id, '') <> '' THEN 1 ELSE 0 END) AS sl_order_rows,
                    SUM(
                        CASE
                            WHEN status = 'OPEN'
                                 AND quantity > 0
                                 AND COALESCE(kotak_sl_order_id, '') = ''
                            THEN 1 ELSE 0
                        END
                    ) AS open_positions_missing_sl
                FROM trades
                WHERE date = ?
                """,
                (today,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.error("EOD trade audit read failed: %s", exc)
        return fallback

    return {key: int(row[key] or 0) for key in fallback}


def _count_today_critical_logs(log_path) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        return sum(
            1
            for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.startswith(today) and "[CRITICAL]" in line
        )
    except OSError:
        return 0


async def run_end_of_day_report():
    """
    Scheduled job: Generate final market close report
    Called at 15:30 PM (market close time)
    
    SUCCESS CRITERIA (what ACTUALLY matters):
      ✅ Screener Ran
      ✅ Briefing Generated
      ✅ Orders Placed
      ✅ SL Attached
      ✅ Squareoff Worked (positions = 0)
      ✅ No Crashes
    
    P&L doesn't matter. System working does.
    """
    try:
        logger.info("\n" + "="*70)
        logger.info("📊 15:30 PM - MARKET CLOSE REPORT (ACTUAL EXECUTION PROOF)")
        logger.info("="*70)
        
        import json
        from pathlib import Path
        from datetime import datetime
        from core.state_manager import get_open_positions, get_today_gross_pnl
        from core.trading_settings import get as cfg
        
        # Collect data
        timestamp = datetime.now().isoformat()
        positions = get_open_positions() or []
        pnl = get_today_gross_pnl()
        
        # Fetch actual capital_start locked in for today
        import sqlite3
        from core.state_manager import DB_PATH
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT capital_start FROM daily_stats WHERE date = ?", (datetime.now().strftime("%Y-%m-%d"),)).fetchone()
            conn.close()
            if row and row[0] is not None:
                capital = float(row[0])
            else:
                capital = 20000.0  # Fallback
        except Exception:
            capital = 20000.0
            
        positions_count = len(positions)
        
        # Check components from actual files and database state.
        briefing = load_briefing()
        briefing_exists = Path("data/session_briefing.json").exists()
        briefing_valid, briefing_reason = validate_briefing(briefing)
        generated_at = str((briefing or {}).get("generated_at") or "")
        screener_ran = (
            briefing_valid
            and (briefing or {}).get("session_type") == "MORNING_SCREENER"
            and generated_at.startswith(datetime.now().strftime("%Y-%m-%d"))
        )
        trade_audit = _read_today_trade_audit()
        orders_count = trade_audit["buy_rows"]
        sl_attached = (
            trade_audit["buy_rows"] > 0
            and trade_audit["open_positions_missing_sl"] == 0
        )
        squareoff_worked = positions_count == 0
        
        # Check for crashes in log
        critical_count = _count_today_critical_logs(Path("data/alcosoft.log"))
        no_crashes = critical_count == 0
        
        # System components
        logger.info("")
        logger.info("SYSTEM COMPONENTS (What matters):")
        logger.info(f"  {'✅' if screener_ran else '❌'} Screener Ran")
        logger.info(f"  {'✅' if briefing_exists else '❌'} Briefing Generated")
        logger.info(f"  {'✅' if orders_count > 0 else '❌'} Orders Placed ({orders_count})")
        logger.info(f"  {'✅' if sl_attached else '❌'} SL Attached")
        logger.info(f"  {'✅' if squareoff_worked else '❌'} Squareoff Worked (Positions: {positions_count})")
        logger.info(f"  {'✅' if no_crashes else '❌'} No Crashes (CRITICAL today: {critical_count})")
        
        # Determine success
        all_components_ok = (
            screener_ran and 
            briefing_exists and 
            squareoff_worked and 
            no_crashes
        )
        
        # P&L (informational only)
        logger.info("")
        logger.info("FINANCIAL METRICS (informational):")
        logger.info(f"  Capital: ₹{capital}")
        logger.info(f"  Final P&L: ₹{pnl} ({round(100*pnl/capital, 2)}%)")
        logger.info(f"  Positions Open: {positions_count}")
        
        # Build report
        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": "15:30 IST",
            "timestamp": timestamp,
            "market_status": "CLOSED",
            "components": {
                "screener_ran": screener_ran,
                "briefing_generated": briefing_exists,
                "briefing_valid": briefing_valid,
                "briefing_reason": briefing_reason,
                "orders_placed": orders_count > 0,
                "sl_attached": sl_attached,
                "squareoff_worked": squareoff_worked,
                "no_crashes": no_crashes,
                "critical_log_count_today": critical_count
            },
            "metrics": {
                "positions_open": positions_count,
                "final_pnl": pnl,
                "pnl_percentage": round(100 * pnl / capital, 2) if capital else 0,
                "capital": capital,
                "trade_audit": trade_audit
            },
            "status": "SUCCESSFUL" if all_components_ok else "PARTIAL"
        }
        
        # Save report
        report_file = Path("data/market_close_report.json")
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)
        
        # Final verdict
        logger.info("")
        if all_components_ok:
            logger.info("✅✅✅ TODAY WAS SUCCESSFUL ✅✅✅")
            logger.info("System worked correctly. P&L = doesn't matter.")
        else:
            failed = []
            if not screener_ran: failed.append("Screener")
            if not briefing_exists or not briefing_valid: failed.append("Briefing")
            if orders_count > 0 and not sl_attached: failed.append("SL attachment")
            if not squareoff_worked: failed.append("Squareoff")
            if not no_crashes: failed.append("Crashes detected")
            logger.warning(f"⚠️  TODAY HAD ISSUES: {', '.join(failed)}")
        
        logger.info("")
        logger.info(f"📁 Report saved: {report_file}")
        logger.info("="*70)
        
    except Exception as e:
        logger.error(f"❌ End-of-day report failed: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════
#   SCHEDULER SETUP
# ════════════════════════════════════════════════════════════

def setup_scheduler() -> AsyncIOScheduler:
    from core.trading_settings import get as cfg

    scheduler = AsyncIOScheduler()
    cognition_interval = int(cfg("scheduling", "cognition_cycle_interval_minutes", 15))

    # Morning screener — 8:45 AM daily
    scheduler.add_job(
        _run_screener_and_refresh_feed,
        trigger="cron",
        hour=8, minute=45,
        id="morning_screener",
        name="Morning Stock Screener",
        max_instances=1,
    )

    # Observation loop — every 15 minutes during market hours
    # Continuous market awareness + signal reliability tracking
    scheduler.add_job(
        run_observation_cycle,
        trigger="interval",
        minutes=cognition_interval,
        id="observation",
        name="Market Observation Loop",
        max_instances=1,
    )

    # Emergency squareoff — 3:15 PM daily
    # Closes all positions before market close to prevent overnight risk
    scheduler.add_job(
        run_emergency_squareoff,
        trigger="cron",
        hour=15, minute=15,
        id="emergency_squareoff",
        name="Emergency Squareoff",
        max_instances=1,
    )

    # Reflection loop — 3:35 PM daily
    scheduler.add_job(
        run_reflection_loop,
        trigger="cron",
        hour=15, minute=35,
        id="reflection",
        name="Owl Alpha Reflection",
        max_instances=1,
    )

    # End-of-day report — 3:30 PM daily
    # Final market close report with P&L and system status
    scheduler.add_job(
        run_end_of_day_report,
        trigger="cron",
        hour=15, minute=30,
        id="eod_report",
        name="End-of-Day Report",
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started:\n"
        f"   08:45 AM - Morning screener\n"
        f"   Every {cognition_interval} min - Observation loop\n"
        f"   15:15 PM - Emergency squareoff (before close)\n"
        f"   15:30 PM - Market close report\n"
        f"   15:35 PM - Reflection cycle"
    )
    return scheduler


async def _run_screener_and_refresh_feed():
    run_morning_screener()
    briefing = load_briefing()
    if briefing:
        approved  = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        watchlist = [s["ticker"] for s in briefing.get("watchlist", [])]
        all_stocks = list(dict.fromkeys(approved + watchlist))
        start_live_feed(all_stocks)
        from core.data_fetcher import fix_briefing_trading_symbols
        fix_briefing_trading_symbols(briefing)
        save_briefing(briefing)
        logger.info(f"Refreshing live feed: {len(all_stocks)} symbols")


# ════════════════════════════════════════════════════════════
#   GRACEFUL SHUTDOWN (FIXED)
# ════════════════════════════════════════════════════════════

def setup_shutdown_handler(scheduler: AsyncIOScheduler):
    """
    Signal handler for Ctrl+C / SIGTERM.

    IMPORTANT: Signal handlers run in a sync context, so we
    cannot await anything here. We set the _shutdown_event
    flag which the async strategy loop is watching.
    The loop exits on its own, then main() cleans up.
    """
    def _shutdown(sig, frame):
        logger.info("Shutdown signal received. Stopping gracefully...")

        # Signal the strategy loop to stop
        if _shutdown_event:
            _shutdown_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


async def _cleanup(scheduler: AsyncIOScheduler):
    logger.info("Running cleanup...")

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")

    # ✅ LIVE FEED PEHLE BAND KARO (session still alive)
    briefing = load_briefing()
    if briefing:
        approved  = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        watchlist = [s["ticker"] for s in briefing.get("watchlist", [])]
        all_stocks = list(dict.fromkeys(approved + watchlist))
        try:
            stop_live_feed(all_stocks)
            logger.info("Live feed stopped.")
        except Exception as e:
            logger.warning(f"Live feed stop error (safe to ignore): {e}")

    # ✅ PHIR LOGOUT KARO
    logout()
    logger.info("Kotak session closed.")

    logger.info("AlcoSoft shutdown complete. Goodbye.")


# ════════════════════════════════════════════════════════════
#   MAIN
# ════════════════════════════════════════════════════════════

async def _health_monitor_loop(shutdown_event: asyncio.Event, interval_sec: int = 300):
    """Background health checks while the strategy loop runs."""
    from core.health_monitor import continuous_monitoring

    while not shutdown_event.is_set():
        try:
            await asyncio.to_thread(continuous_monitoring)
        except Exception as e:
            logger.error(f"Health monitor error: {e}")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass


async def main():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    success = await startup()
    if not success:
        return

    scheduler = setup_scheduler()
    setup_shutdown_handler(scheduler)

    logger.info("Starting strategy loop. Press Ctrl+C to shutdown safely.")

    monitor_task = asyncio.create_task(_health_monitor_loop(_shutdown_event))

    try:
        await run_strategy_loop(_shutdown_event)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    await _cleanup(scheduler)


if __name__ == "__main__":
    asyncio.run(main())
