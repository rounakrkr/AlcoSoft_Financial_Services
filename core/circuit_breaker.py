# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/circuit_breaker.py — System Stability Guardian
#   Halts trading on critical errors, prevents cascading failures
# ============================================================

import logging
import time
from enum import Enum
from datetime import datetime, timedelta
from typing import Callable, Any

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"         # Tripped, blocking calls
    HALF_OPEN = "HALF_OPEN"  # Testing if recovered


class CircuitBreaker:
    """
    Stops cascading failures by failing fast after error threshold.
    
    States:
    - CLOSED: Normal, pass through calls
    - OPEN: Error threshold exceeded, block calls, return default
    - HALF_OPEN: Cool down period passed, test one call
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
    
    def is_open(self) -> bool:
        """Check if circuit is open (blocking)."""
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
                logger.warning(f"🔌 [{self.name}] Circuit HALF_OPEN - testing recovery...")
                return False
            return True
        return False
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to retry."""
        if not self.last_failure_time:
            return False
        elapsed = (datetime.now() - self.last_failure_time).total_seconds()
        return elapsed >= self.recovery_timeout
    
    def call(self, func: Callable, *args, default=None, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        if self.is_open():
            logger.error(f"❌ [{self.name}] Circuit OPEN - blocking call (returning default)")
            return default
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            logger.error(f"❌ [{self.name}] Call failed: {e} ({self.failure_count}/{self.failure_threshold})")
            if self.state == CircuitState.OPEN:
                return default
            raise
    
    def _on_success(self):
        """Record successful call."""
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"✅ [{self.name}] Recovery successful - circuit CLOSED")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = None
        else:
            self.failure_count = 0
            self.success_count += 1
    
    def _on_failure(self):
        """Record failed call."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.critical(
                f"🔴 [{self.name}] Circuit OPEN after {self.failure_count} failures - "
                f"will retry in {self.recovery_timeout}s"
            )
    
    def reset(self):
        """Manually reset the circuit."""
        logger.info(f"🔄 [{self.name}] Manual reset")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None


# Global circuit breakers for critical systems
breakers = {
    "broker": CircuitBreaker("Broker API", failure_threshold=2, recovery_timeout=30),
    "order": CircuitBreaker("Order Execution", failure_threshold=3, recovery_timeout=60),
    "data_feed": CircuitBreaker("Market Data Feed", failure_threshold=5, recovery_timeout=120),
    "strategy": CircuitBreaker("Strategy Loop", failure_threshold=5, recovery_timeout=120),
}


def get_breaker(name: str) -> CircuitBreaker:
    """Get or create a circuit breaker."""
    if name not in breakers:
        breakers[name] = CircuitBreaker(name)
    return breakers[name]


def get_status() -> dict:
    """Get status of all circuit breakers."""
    return {
        name: {
            "state": breaker.state.value,
            "failures": breaker.failure_count,
            "last_failure": breaker.last_failure_time.isoformat() if breaker.last_failure_time else None,
        }
        for name, breaker in breakers.items()
    }


def halt_all_trading(reason: str):
    """Emergency halt - open all trading circuits."""
    logger.critical(f"🛑 EMERGENCY HALT: {reason}")
    for breaker in breakers.values():
        breaker.state = CircuitState.OPEN
    logger.critical("All trading circuits are OPEN - system halted")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
