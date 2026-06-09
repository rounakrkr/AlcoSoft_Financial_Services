import sys, os, asyncio, logging
sys.path.insert(0, '/home/ubuntu/alcosoft')

from reflection.observation_loop import run_observation_cycle
import core.market_calendar
from datetime import time

logging.basicConfig(level=logging.INFO)

# Force market to be CLOSED by setting open time to Future
core.market_calendar.MARKET_OPEN = time(12, 0)
core.market_calendar.MARKET_CLOSE = time(12, 0)

asyncio.run(run_observation_cycle())
