import os
import sys
import time
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.kotak_client import get_client

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Very active liquid stock to guarantee fast ticks
TEST_INSTRUMENT = "RELIANCE"

def capture_ticks():
    client = get_client()
    logger.info(f"Connecting to Kotak Neo WebSocket to capture {TEST_INSTRUMENT}...")
    
    # In AlcoSoft, we likely use the client's subscribe method.
    # Kotak Neo API usually uses callbacks for websocket data.
    
    tick_count = 0
    max_ticks = 10
    
    def on_message(message):
        nonlocal tick_count
        if tick_count >= max_ticks:
            return
            
        # Parse Kotak Neo tick payload
        # Some brokers return a list of ticks, some return a single dict
        ticks = message if isinstance(message, list) else [message]
        
        for tick in ticks:
            # Dump the ENTIRE raw tick payload to conclusively determine ALL available fields
            import json
            logger.info(f"RAW TICK CAPTURED:\n{json.dumps(tick, indent=2)}")
            tick_count += 1
            
            if tick_count >= max_ticks:
                logger.info("\n✅ 10 consecutive raw ticks captured successfully. Closing feed...")
                os._exit(0)

    def on_error(error):
        logger.error(f"WebSocket Error: {error}")

    def on_close(message):
        logger.info(f"WebSocket Closed: {message}")

    def on_open(message):
        logger.info(f"WebSocket Opened! {message}")
        # Kotak Neo API typically subscribes via instrument token
        # We need the token for RELIANCE-EQ, but the API can usually lookup
        # If your kotak_client wrapper handles this:
        try:
            client.subscribe(instrument_tokens=[TEST_INSTRUMENT], isIndex=False, isDepth=False)
        except Exception as e:
            logger.error(f"Subscription failed: {e}")

    try:
        # Assuming client exposes standard websocket hooks
        client.on_message = on_message
        client.on_error = on_error
        client.on_close = on_close
        client.on_open = on_open
        
        # Start connection by subscribing
        from core.data_fetcher import resolve_instrument_tokens
        instrument_tokens = resolve_instrument_tokens([TEST_INSTRUMENT])
        if not instrument_tokens:
            logger.error("Failed to resolve instrument token. Aborting.")
            return

        logger.info(f"Subscribing to {instrument_tokens}...")
        client.subscribe(
            instrument_tokens=instrument_tokens,
            isIndex=False,
            isDepth=False
        )
        
        # Keep main thread alive while websocket runs in background
        logger.info("Waiting for ticks...")
        while tick_count < max_ticks:
            time.sleep(1)
            
    except Exception as e:
        logger.error(f"Failed to start WebSocket: {e}")

if __name__ == "__main__":
    capture_ticks()
