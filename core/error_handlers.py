#!/usr/bin/env python3
"""
Centralized error handling for ALCOSOFT.
Provides recovery mechanisms and graceful degradation.
"""

import logging
import functools
import traceback
from typing import Callable, Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar('T')

class AlcoSoftError(Exception):
    """Base exception for all ALCOSOFT errors."""
    pass

class OrderExecutionError(AlcoSoftError):
    """Raised when order execution fails."""
    pass

class DataFetchError(AlcoSoftError):
    """Raised when data fetching fails."""
    pass

def safe_execute(func: Callable[..., T], *args, **kwargs) -> T | None:
    """Safely execute a function with error handling."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Error in {func.__name__}: {e}")
        logger.debug(traceback.format_exc())
        return None

def retry_on_error(max_retries: int = 3, delay: float = 1.0):
    """Decorator to retry a function on error."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T | None:
            import time
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        logger.error(f"All {max_retries} attempts failed for {func.__name__}")
            
            return None
        return wrapper
    return decorator

def handle_gracefully(func: Callable[..., T], default: Any = None) -> T | Any:
    """Execute function gracefully, returning default on error."""
    try:
        return func()
    except Exception as e:
        logger.warning(f"Function failed gracefully: {e}")
        return default
