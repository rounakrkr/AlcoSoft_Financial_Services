import logging
import time

class DuplicateFilter(logging.Filter):
    """
    A logging filter that drops duplicate log messages emitted within a
    configured cooldown period.
    
    This is specifically designed to eliminate overnight Telegram notification
    spam such as "Observation loop skipping: Market is closed."
    
    Critical messages (WARNING, ERROR, CRITICAL) bypass this filter
    and are always emitted.
    """
    
    def __init__(self, cooldown_seconds: int = 3600, target_loggers: list = None):
        super().__init__()
        self.cooldown_seconds = cooldown_seconds
        self.last_log_times = {}
        self.target_loggers = target_loggers

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. Bypass deduplication for WARNING, ERROR, CRITICAL
        # We NEVER want to suppress real production problems or Capital API failures.
        if record.levelno >= logging.WARNING:
            return True
            
        # 2. Bypass deduplication if logger is not in the target list
        # This guarantees we never hide legitimate identical trade logs from other modules.
        if self.target_loggers and record.name not in self.target_loggers:
            return True
            
        # 3. Extract exactly the message string
        msg = record.getMessage()
        
        current_time = time.time()
        
        # Periodic cleanup to prevent unbounded cache growth (memory leak fix)
        # We clean up every ~cooldown_seconds / 2 to keep dictionary small
        if not hasattr(self, '_last_cleanup'):
            self._last_cleanup = current_time
            
        if current_time - self._last_cleanup > (self.cooldown_seconds / 2):
            self.last_log_times = {
                k: v for k, v in self.last_log_times.items() 
                if current_time - v < self.cooldown_seconds
            }
            self._last_cleanup = current_time
        
        # 3. Check if we've seen this exact message within the cooldown window
        if msg in self.last_log_times:
            elapsed = current_time - self.last_log_times[msg]
            if elapsed < self.cooldown_seconds:
                return False  # Suppress the log
                
        # 4. If new or expired cooldown, record the time and let it pass
        self.last_log_times[msg] = current_time
        return True
