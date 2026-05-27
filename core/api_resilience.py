# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/api_resilience.py — Retry Logic, Rate Limiting, Backoff
#   Wraps external API calls with production-grade resilience
# ============================================================

import logging
import time
import random
from typing import Callable, Any, Optional, TypeVar, Tuple
from functools import wraps
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimiter:
    """Token bucket rate limiter."""
    
    def __init__(self, calls_per_minute: int = 60):
        self.calls_per_minute = calls_per_minute
        self.tokens = float(calls_per_minute)
        self.last_refill = datetime.now()
        self.lock = __import__("threading").Lock()
    
    def acquire(self, tokens: int = 1) -> float:
        """
        Acquire tokens. Returns wait time in seconds.
        0 = no wait needed.
        """
        with self.lock:
            # Refill bucket
            now = datetime.now()
            elapsed = (now - self.last_refill).total_seconds()
            refill_rate = self.calls_per_minute / 60.0
            self.tokens += elapsed * refill_rate
            self.tokens = min(self.tokens, float(self.calls_per_minute))
            self.last_refill = now
            
            # Check if we have enough tokens
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            
            # Wait time needed
            wait_seconds = (tokens - self.tokens) / refill_rate
            self.tokens = 0
            return wait_seconds


# Rate limiters per service
rate_limiters = {
    "kotak_api": RateLimiter(calls_per_minute=30),
    "gemini_api": RateLimiter(calls_per_minute=15),
    "groq_api": RateLimiter(calls_per_minute=30),
    "openrouter_api": RateLimiter(calls_per_minute=60),
}


def get_limiter(service: str) -> RateLimiter:
    """Get rate limiter for service."""
    if service not in rate_limiters:
        rate_limiters[service] = RateLimiter(calls_per_minute=60)
    return rate_limiters[service]


def retry_with_backoff(
    func: Callable[..., T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Tuple = (Exception,),
) -> T:
    """
    Call function with exponential backoff retry.
    
    Args:
        func: Function to call
        max_attempts: Max number of attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Exponent for backoff (2.0 = double each time)
        jitter: Add random jitter to delay
        retryable_exceptions: Exceptions to retry on
    
    Returns:
        Function result
        
    Raises:
        Last exception if all attempts fail
    """
    last_exception = None
    
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        
        except retryable_exceptions as e:
            last_exception = e
            
            if attempt == max_attempts:
                logger.error(f"❌ All {max_attempts} attempts failed for {func.__name__}: {e}")
                raise
            
            # Calculate backoff
            delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
            if jitter:
                delay *= (0.5 + random.random())  # 0.5-1.5x multiplier
            
            logger.warning(
                f"⚠️  Attempt {attempt} failed ({type(e).__name__}): {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
    
    raise last_exception


def with_rate_limit(service: str, tokens: int = 1):
    """Decorator to apply rate limiting."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            limiter = get_limiter(service)
            wait = limiter.acquire(tokens)
            if wait > 0:
                time.sleep(wait)
            return func(*args, **kwargs)
        return wrapper
    return decorator


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retryable_exceptions: Tuple = (Exception,),
):
    """Decorator to apply retry logic."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            def call():
                return func(*args, **kwargs)
            
            return retry_with_backoff(
                call,
                max_attempts=max_attempts,
                base_delay=base_delay,
                retryable_exceptions=retryable_exceptions,
            )
        return wrapper
    return decorator


class ResilientAPICall:
    """Context manager for resilient API calls."""
    
    def __init__(
        self,
        name: str,
        service: str = "default",
        max_attempts: int = 3,
        timeout: float = 30.0,
    ):
        self.name = name
        self.service = service
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        logger.debug(f"🔄 Starting API call: {self.name}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.start_time
        
        if exc_type is None:
            logger.debug(f"✅ {self.name} completed in {elapsed:.2f}s")
            return False
        
        # Log the error
        logger.error(
            f"❌ {self.name} failed after {elapsed:.2f}s: {exc_type.__name__}: {exc_val}"
        )
        
        # Don't suppress the exception (return False)
        return False


# Example resilient API call patterns

def call_broker_api(func: Callable, *args, **kwargs):
    """Safely call Kotak broker API."""
    from core.circuit_breaker import get_breaker
    
    def wrapped():
        with ResilientAPICall("broker_call", service="kotak_api"):
            return func(*args, **kwargs)
    
    breaker = get_breaker("broker")
    return breaker.call(
        wrapped,
        default=None,
    )


def call_ai_api(
    service: str,
    func: Callable,
    *args,
    max_attempts: int = 2,
    **kwargs,
):
    """Safely call AI API with rate limiting and retry."""
    @with_rate_limit(service, tokens=1)
    @with_retry(max_attempts=max_attempts, base_delay=2.0)
    def wrapped():
        with ResilientAPICall("ai_call", service=service):
            return func(*args, **kwargs)
    
    try:
        return wrapped()
    except Exception as e:
        logger.error(f"AI API failed: {e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    # Test retry logic
    attempt = [0]
    def flaky_function():
        attempt[0] += 1
        if attempt[0] < 2:
            raise ValueError("Simulated failure")
        return "Success!"
