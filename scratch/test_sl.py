import sys, asyncio, logging
sys.path.insert(0, 'c:/Extra Programs/Files/AlcoSoft_Financial_Services')

import core.market_calendar
from datetime import time
from main import run_strategy_loop
from core.trading_settings import get as cfg

logging.basicConfig(level=logging.INFO)

# Force market OPEN
core.market_calendar.MARKET_OPEN = time(0, 0)
core.market_calendar.MARKET_CLOSE = time(23, 59)

async def test_run():
    # Only run one iteration to test the BUY + SL placement
    try:
        event = asyncio.Event()
        await asyncio.wait_for(run_strategy_loop(event), timeout=25.0)
    except asyncio.TimeoutError:
        pass

if __name__ == "__main__":
    asyncio.run(test_run())
