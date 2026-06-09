import sys, asyncio, logging
sys.path.insert(0, 'c:/Extra Programs/Files/AlcoSoft_Financial_Services')

import core.order_executor
from core.trading_settings import get as cfg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_runner")

def run():
    logger.info("Executing mock BUY + SL Limit order placement...")
    
    # We pass the symbol from the user's test
    try:
        core.order_executor.place_buy_order(
            symbol="WIPRO",
            trading_symbol="WIPRO-EQ",
            entry_price=181.5,
            stop_loss=180.59,
            strategy="TEST_DEBUG_STRATEGY",
            confidence=85
        )
        logger.info("Mock execution completed!")
    except Exception as e:
        logger.error(f"Execution failed: {e}", exc_info=True)

if __name__ == "__main__":
    run()
