# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/cognition_scheduler.py — Integration with Strategy Loop
#
#   Shows how to integrate cognitive observation loop into main strategy.
#   Call this from your main strategy loop or task scheduler.
# ============================================================

import logging
from datetime import datetime, time as dt_time
from threading import Thread, Timer

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
#   COGNITIVE SCHEDULER INTEGRATION
# ════════════════════════════════════════════════════════════
#
#   IMPORTANT: MARKET-TIED COGNITION (NOT TRADE-TIED)
#
#   Execution engine stops trading at 3:00 PM,
#   but cognition agents continue observing until 3:15 PM.
#
#   Reason:
#   - Closing volatility reveals market structure
#   - Final-hour institutional behavior patterns matter
#   - Regime shifts often occur late session
#   - End-of-day market structure valuable for next-day cognition
#
#   Timeline:
#   - 9:30 AM - 3:15 PM: Cognition agents observe every 15 minutes
#   - 3:00 PM: Execution stops taking new trades
#   - 3:15 PM: Last cognition observation cycle (Agent D)
#   - 3:30 PM: Market officially closes
#   - 3:35 PM: Final Reflection Agent synthesizes the day
# ════════════════════════════════════════════════════════════

_cognitive_timer: Timer | None = None
_is_running = False


def is_market_hours() -> bool:
    """Check if market is currently open."""
    now = datetime.now().time()
    market_open = dt_time(9, 15)
    market_close = dt_time(15, 30)
    return market_open <= now < market_close


def is_cognitive_cycle_time() -> bool:
    """Check if current time matches cognitive cycle schedule (9:30 AM - 3:15 PM)."""
    from core.trading_settings import get as cfg

    now = datetime.now().time()
    market_open = dt_time(9, 15)
    first_cycle = dt_time(9, 30)
    last_cycle = dt_time(15, 15)  # Stop cognition at 3:15 PM (before market close at 3:30 PM)

    # Don't run outside cognition hours
    if now < market_open or now > last_cycle:
        return False

    # Get cycle interval from config (default 15 minutes)
    cycle_interval = int(cfg("scheduling", "cognition_cycle_interval_minutes", 15))

    # Calculate minutes since first cycle
    current_minute = now.hour * 60 + now.minute
    first_minute = first_cycle.hour * 60 + first_cycle.minute

    if current_minute < first_minute:
        return False

    minutes_since_first = current_minute - first_minute

    # Check if we're at a cycle boundary matching the interval
    return minutes_since_first % cycle_interval == 0


def schedule_cognitive_cycle():
    """
    Non-blocking cognitive cycle scheduler.
    Call this from your main strategy loop or a separate scheduler.
    
    Option 1: Add to main strategy loop
    ```
    def strategy_loop():
        while trading_active:
            # ... main strategy code ...
            schedule_cognitive_cycle()  # Add this
            time.sleep(5)
    ```
    
    Option 2: Use APScheduler
    ```
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(schedule_cognitive_cycle, 'cron', second=0, max_instances=1)
    scheduler.start()
    ```
    """
    if not is_market_hours():
        return
    
    if is_cognitive_cycle_time():
        try:
            from reflection.cognitive_agents import run_cognitive_observation_cycle
            logger.debug("🧠 Cognitive cycle trigger detected")
            run_cognitive_observation_cycle()
        except Exception as e:
            logger.warning(f"Cognitive cycle execution failed: {e}")


def run_cognitive_scheduler_background():
    """
    Run cognitive scheduler in background thread.
    Checks every minute for cognitive cycle times.
    """
    global _cognitive_timer, _is_running
    
    if _is_running:
        return
    
    _is_running = True
    logger.info("🧠 Cognitive observation scheduler started (background thread)")
    
    def scheduler_loop():
        global _cognitive_timer
        
        schedule_cognitive_cycle()
        
        # Schedule next check in 60 seconds
        _cognitive_timer = Timer(60.0, scheduler_loop)
        _cognitive_timer.daemon = True
        _cognitive_timer.start()
    
    scheduler_loop()


def stop_cognitive_scheduler():
    """Stop background scheduler."""
    global _cognitive_timer, _is_running
    
    if _cognitive_timer:
        _cognitive_timer.cancel()
        _cognitive_timer = None
    
    _is_running = False
    logger.info("🧠 Cognitive observation scheduler stopped")


def register_cognitive_cycle_with_apscheduler(scheduler):
    """
    Register cognitive cycle with existing APScheduler instance.
    
    Example:
    ```
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    register_cognitive_cycle_with_apscheduler(scheduler)
    scheduler.start()
    ```
    """
    from reflection.cognitive_agents import run_cognitive_observation_cycle
    
    # Run cognitive cycle check every minute
    scheduler.add_job(
        run_cognitive_observation_cycle,
        'cron',
        second=0,
        max_instances=1,
        id='cognitive_cycle_check'
    )
    
    logger.info("🧠 Cognitive cycle registered with APScheduler")


# ════════════════════════════════════════════════════════════
#   FINAL REFLECTION SCHEDULING
# ════════════════════════════════════════════════════════════

def schedule_final_reflection(scheduler=None):
    """
    Schedule final reflection agent to run at 3:35 PM daily (after market close).
    
    TIMING:
    - Execution stops: 3:00 PM
    - Last cognition cycle: 3:15 PM
    - Market closes: 3:30 PM
    - Final Reflection: 3:35 PM (all data complete)
    
    Option 1: Manual check in main loop
    ```
    def after_market_close():
        schedule_final_reflection()
    ```
    
    Option 2: With APScheduler
    ```
    scheduler.add_job(
        run_final_reflection,
        'cron',
        hour=15, minute=35,
        id='final_reflection'
    )
    ```
    """
    from reflection.reflection_loop import run_reflection_loop
    
    now = datetime.now()
    reflection_time = datetime.now().replace(hour=15, minute=35, second=0, microsecond=0)
    
    # If past reflection time today, don't run
    if now > reflection_time:
        logger.debug("Past reflection time for today")
        return
    
    if scheduler:
        # Using APScheduler
        scheduler.add_job(
            run_reflection_loop,
            'cron',
            hour=15,
            minute=35,
            id='final_reflection',
            replace_existing=True
        )
        logger.info("✅ Final reflection scheduled for 15:35 (3:35 PM) with APScheduler")
    else:
        # Check if we should run now
        now_time = now.time()
        reflection_start = dt_time(15, 34)  # 3:34 PM
        reflection_end = dt_time(15, 36)    # 3:36 PM
        
        if reflection_start <= now_time <= reflection_end:
            logger.info("⏰ Running final reflection (time window matched)")
            run_reflection_loop()


def run_final_reflection():
    """Wrapper for final reflection (compatible with schedulers)."""
    from reflection.reflection_loop import run_reflection_loop
    
    try:
        run_reflection_loop()
    except Exception as e:
        logger.error(f"Final reflection failed: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════
#   SIMPLIFIED USAGE EXAMPLES
# ════════════════════════════════════════════════════════════

"""
OPTION A: Add to existing strategy loop
================================================

In your main strategy.py or similar:

    from reflection.cognition_scheduler import schedule_cognitive_cycle
    
    def strategy_loop():
        while market_is_open:
            # ... your strategy code ...
            
            # Add cognitive cycle check (runs every 15 min during market hours)
            schedule_cognitive_cycle()
            
            time.sleep(5)


OPTION B: Standalone APScheduler
================================================

Create a new scheduler file:

    from apscheduler.schedulers.background import BackgroundScheduler
    from reflection.cognition_scheduler import register_cognitive_cycle_with_apscheduler, run_final_reflection
    
    scheduler = BackgroundScheduler()
    register_cognitive_cycle_with_apscheduler(scheduler)
    
    # Schedule final reflection at 3:15 PM
    scheduler.add_job(
        run_final_reflection,
        'cron',
        hour=15,
        minute=15,
        id='final_reflection'
    )
    
    scheduler.start()


OPTION C: Background thread
================================================

    from reflection.cognition_scheduler import run_cognitive_scheduler_background, schedule_final_reflection
    
    # Start background scheduler
    run_cognitive_scheduler_background()
    
    # In your main loop
    def after_market_close():
        schedule_final_reflection()


OPTION D: Direct integration with strategy.py
================================================

Modify core/strategy.py strategy_loop():

    def strategy_loop():
        from reflection.cognition_scheduler import schedule_cognitive_cycle
        
        while True:
            # ... existing strategy code ...
            
            # Check and run cognitive cycles
            try:
                schedule_cognitive_cycle()
            except Exception as e:
                logger.warning(f"Cognitive cycle skipped: {e}")
            
            time.sleep(5)

"""
