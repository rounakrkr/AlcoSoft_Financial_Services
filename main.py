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
from core.state_manager import initialize_db, recover_state, load_briefing
from core.data_fetcher import start_live_feed, stop_live_feed
from core.strategy import run_strategy_loop
from screener.morning_screener import run_morning_screener
from war_room.orchestrator import run_war_room
from reflection.reflection_loop import run_reflection_loop


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

    # Step 1 — Database
    logger.info("[1/5] Initializing database...")
    initialize_db()

    # Step 2 — Crash Recovery
    logger.info("[2/5] Checking for previous state...")
    state = recover_state()

    if state["open_position_count"] > 0:
        logger.warning(
            f"RECOVERY: {state['open_position_count']} open position(s) "
            f"from last session. Monitoring resumed."
        )

    # Step 3 — Kotak Login
    logger.info("[3/5] Connecting to Kotak Neo...")
    try:
        client = get_client()
        logger.info("Kotak session established successfully.")
    except Exception as e:
        logger.critical(f"Kotak login failed: {e}")
        logger.critical("Cannot proceed without broker connection. Exiting.")
        sys.exit(1)

    # Step 4 — Morning Screener (if pre-market)
    now = datetime.now().time()
    if now < MARKET_OPEN:
        logger.info("[4/5] Pre-market: Running morning screener...")
        try:
            run_morning_screener()
        except Exception as e:
            logger.error(f"Morning screener failed: {e}")
            logger.warning("Will use existing briefing if available.")
    else:
        logger.info("[4/5] Market already open. Skipping screener.")

    # Step 5 — Load briefing and start data feed
    logger.info("[5/5] Loading briefing and starting live feed...")
    briefing = load_briefing()

    if briefing:
        stocks = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        logger.info(f"Starting live feed for: {stocks}")
        start_live_feed(stocks)
    else:
        logger.warning("No briefing available. Waiting for screener/war room...")

    logger.info("Startup complete. System is LIVE.")
    return True


# ════════════════════════════════════════════════════════════
#   SCHEDULER SETUP
# ════════════════════════════════════════════════════════════

def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    # Morning screener — 8:45 AM daily
    scheduler.add_job(
        _run_screener_and_refresh_feed,
        trigger="cron",
        hour=8, minute=45,
        id="morning_screener",
        name="Morning Stock Screener",
        max_instances=1,
    )

    # War room — every N minutes during market hours
    war_room_interval = int(os.getenv("WAR_ROOM_INTERVAL_MINUTES", 30))
    scheduler.add_job(
        run_war_room,
        trigger="interval",
        minutes=war_room_interval,
        id="war_room",
        name="AlcoSoft War Room",
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
        f"   War room          : Every {war_room_interval} minutes\n"
        f"   Reflection loop   : 03:35 PM daily"
    )
    return scheduler


async def _run_screener_and_refresh_feed():
    run_morning_screener()
    briefing = load_briefing()
    if briefing:
        stocks = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        logger.info(f"Refreshing live feed: {stocks}")
        start_live_feed(stocks)


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
    """Runs after strategy loop exits. Safe async context."""
    logger.info("Running cleanup...")

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")

    logout()
    logger.info("Kotak session closed.")

    briefing = load_briefing()
    if briefing:
        stocks = [s["ticker"] for s in briefing.get("approved_stocks", [])]
        stop_live_feed(stocks)
    logger.info("Live feed stopped.")

    logger.info("AlcoSoft shutdown complete. Goodbye.")


# ════════════════════════════════════════════════════════════
#   MAIN
# ════════════════════════════════════════════════════════════

async def main():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    success = await startup()
    if not success:
        return

    scheduler = setup_scheduler()
    setup_shutdown_handler(scheduler)

    logger.info("Starting strategy loop. Press Ctrl+C to shutdown safely.")

    # Run strategy loop — it receives the shutdown event and
    # exits cleanly when _shutdown_event is set.
    await run_strategy_loop(_shutdown_event)

    # Strategy loop has exited — now clean up
    await _cleanup(scheduler)


if __name__ == "__main__":
    asyncio.run(main())