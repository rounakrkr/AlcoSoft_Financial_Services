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


# ── Imports (after logging setup) ─────────────────────────────
from core.kotak_client import get_client, logout
from core.state_manager import initialize_db, recover_state, load_briefing, save_briefing
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

    # STEP 1 — Preflight Checks (before any trading)
    logger.info("[1/6] Running preflight health checks...")
    from core.health_monitor import run_preflight_checks
    health = run_preflight_checks()
    
    if os.getenv('TRADING_MODE', 'PAPER') == 'LIVE' and not health.passed():
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

    # Step 4 — Morning Screener (if pre-market OR briefing missing)
    now = datetime.now().time()
    screener_success = False
    
    # RUN SCREENER IF:
    # 1. It's before market open (normal 8:45 AM case), OR
    # 2. Briefing is missing/empty (recovery case - system started late)
    briefing_status = load_briefing()
    briefing_stocks = 0
    if briefing_status:
        briefing_stocks = len(briefing_status.get("approved_stocks", [])) + len(briefing_status.get("watchlist", []))
    
    should_run_screener = (now < MARKET_OPEN) or (briefing_stocks == 0)
    
    if should_run_screener:
        if now < MARKET_OPEN:
            logger.info("[4/6] Pre-market: Running morning screener...")
        else:
            logger.info("[4/6] Briefing empty: Running screener recovery...")
        try:
            screener_success = run_morning_screener()
            if screener_success:
                logger.info("✅ Screener completed successfully")
            else:
                logger.warning("⚠️  Screener encountered errors")
        except Exception as e:
            logger.error(f"Screener exception: {e}")
            logger.warning("Will verify existing briefing or attempt regeneration.")
    else:
        logger.info("[4/6] Market already open & briefing present. Skipping screener.")

    # Step 5 — Load briefing and start data feed
    logger.info("[5/6] Loading briefing and starting live feed...")
    briefing = load_briefing()
    
    # VERIFICATION: If briefing is missing or invalid, attempt regeneration ONCE
    briefing_stocks_count = 0
    if briefing:
        approved = briefing.get("approved_stocks", [])
        watchlist = briefing.get("watchlist", [])
        briefing_stocks_count = len(approved) + len(watchlist)
        logger.info(f"Briefing status: {len(approved)} approved + {len(watchlist)} watchlist")
    
    # Only attempt regeneration if screener hadn't run yet (time-based check)
    already_ran_screener = should_run_screener and screener_success
    
    if (not briefing or briefing_stocks_count == 0) and not already_ran_screener:
        logger.warning("⚠️  Briefing invalid or missing. Attempting regeneration...")
        logger.info("   Triggering screener regeneration (attempt 1)...")
        try:
            regen_success = run_morning_screener()
            if regen_success:
                briefing = load_briefing()
                if briefing:
                    approved = briefing.get("approved_stocks", [])
                    watchlist = briefing.get("watchlist", [])
                    briefing_stocks_count = len(approved) + len(watchlist)
                    logger.info(f"✅ Regeneration successful: {len(approved)} approved + {len(watchlist)} watchlist")
                else:
                    logger.error("❌ Screener claimed success but briefing still missing")
            else:
                logger.error("❌ Screener regeneration failed")
        except Exception as e:
            logger.error(f"Screener regeneration exception: {e}")
    elif briefing_stocks_count > 0:
        logger.info("✅ Briefing valid and populated - ready for trading")

    if briefing and briefing_stocks_count > 0:
        approved  = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        watchlist = [s["ticker"] for s in briefing.get("watchlist", [])]
        all_stocks = list(dict.fromkeys(approved + watchlist))

        if all_stocks:
            from core.data_fetcher import purge_invalid_token_cache
            purge_invalid_token_cache()
            logger.info(
                f"Subscribing live feed: {len(all_stocks)} symbols "
                f"({len(approved)} legacy + {len(watchlist)} math/technical)"
            )
            start_live_feed(all_stocks)

            # ✅ AB trading symbol fix karo (cache ready)
            from core.data_fetcher import fix_briefing_trading_symbols
            fix_briefing_trading_symbols(briefing)
            save_briefing(briefing)

            logger.info(f"Live feed active for {len(all_stocks)} symbols.")
            if approved:
                logger.info(f"  Legacy stocks : {approved}")
            logger.info(f"  Math watchlist: {watchlist}")
        else:
            logger.warning("Briefing found but no stocks to trade. Waiting for updates...")
    else:
        logger.error("❌ BRIEFING UNAVAILABLE: Cannot enable trading")
        if not briefing:
            logger.error("   Reason: Briefing file does not exist or could not be loaded")
        elif briefing_stocks_count == 0:
            logger.error("   Reason: Briefing is empty (no approved_stocks or watchlist)")
        logger.critical("Cannot proceed to trading without valid briefing. Exiting.")
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

    # Reflection loop — 3:35 PM daily
    scheduler.add_job(
        run_reflection_loop,
        trigger="cron",
        hour=15, minute=35,
        id="reflection",
        name="Owl Alpha Reflection",
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started:\n"
        f"   Morning screener  : 08:45 AM daily\n"
        f"   Observation loop  : Every {cognition_interval} minutes\n"
        f"   Reflection loop   : 03:35 PM daily"
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
