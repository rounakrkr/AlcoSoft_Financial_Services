#!/usr/bin/env python3
# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   test_briefing_lifecycle_hardening.py
#
#   COMPREHENSIVE BRIEFING LIFECYCLE TEST
#
#   Simulates 4 critical startup scenarios:
#   - Case A: Briefing missing at startup
#   - Case B: Briefing empty (no stocks)
#   - Case C: Briefing valid (has stocks)
#   - Case D: TEST_* briefing present
#
#   Each case validates the full cycle:
#   Load → Validate → Decision → Outcome
# ============================================================

import json
import os
import sys
import logging
from datetime import datetime

# Setup path
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("BriefingLifecycleTest")

from core.state_manager import (
    validate_briefing,
    is_briefing_safe_for_trading,
    load_briefing,
    save_briefing,
    BRIEFING_PATH,
    FALLBACK_BRIEFING,
)

# ════════════════════════════════════════════════════════════
#   TEST UTILITIES
# ════════════════════════════════════════════════════════════

def backup_briefing():
    """Backup current briefing if it exists."""
    if os.path.exists(BRIEFING_PATH):
        backup_path = BRIEFING_PATH.replace(".json", ".backup.json")
        with open(BRIEFING_PATH, 'r') as src:
            with open(backup_path, 'w') as dst:
                dst.write(src.read())
        logger.info(f"Backed up briefing to {backup_path}")
        return backup_path
    return None

def restore_briefing(backup_path):
    """Restore briefing from backup."""
    if backup_path and os.path.exists(backup_path):
        with open(backup_path, 'r') as src:
            with open(BRIEFING_PATH, 'w') as dst:
                dst.write(src.read())
        os.remove(backup_path)
        logger.info(f"Restored briefing from {backup_path}")

def delete_briefing():
    """Delete briefing file."""
    if os.path.exists(BRIEFING_PATH):
        os.remove(BRIEFING_PATH)
        logger.info(f"Deleted briefing file: {BRIEFING_PATH}")

def create_empty_briefing():
    """Create empty briefing (no stocks)."""
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST_EMPTY",
        "market_bias": "NEUTRAL",
        "approved_stocks": [],
        "watchlist": [],
        "avoid_list": [],
    }
    save_briefing(briefing)
    logger.info("Created empty briefing")
    return briefing

def create_valid_briefing():
    """Create valid briefing with stocks."""
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "MORNING_SCREENER",
        "market_bias": "BULLISH",
        "approved_stocks": [
            {"ticker": "INFY", "trading_symbol": "INFY-EQ", "direction": "BUY_ONLY", "confidence": 75},
            {"ticker": "TCS", "trading_symbol": "TCS-EQ", "direction": "BUY_ONLY", "confidence": 70},
        ],
        "watchlist": [
            {"ticker": "RELIANCE", "trading_symbol": "RELIANCE-EQ", "direction": "WATCH", "confidence": 65},
            {"ticker": "HDFCBANK", "trading_symbol": "HDFCBANK-EQ", "direction": "WATCH", "confidence": 60},
        ],
        "avoid_list": [],
    }
    save_briefing(briefing)
    logger.info("Created valid briefing")
    return briefing

def create_test_briefing():
    """Create TEST_* briefing (should be rejected)."""
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST_MANUAL_SETUP",
        "market_bias": "NEUTRAL",
        "approved_stocks": [
            {"ticker": "INFY", "trading_symbol": "INFY-EQ", "direction": "BUY_ONLY", "confidence": 75},
        ],
        "watchlist": [],
        "avoid_list": [],
    }
    save_briefing(briefing)
    logger.info("Created TEST briefing")
    return briefing

def create_placeholder_briefing():
    """Create placeholder briefing (marked DO NOT USE)."""
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "PLACEHOLDER_AWAITING_SCREENER",
        "market_bias": "NEUTRAL",
        "approved_stocks": [],
        "watchlist": [],
        "avoid_list": [],
        "do_not_use_for_trading": True,
    }
    save_briefing(briefing)
    logger.info("Created placeholder briefing")
    return briefing

# ════════════════════════════════════════════════════════════
#   TEST CASES
# ════════════════════════════════════════════════════════════

def test_case_a_missing_briefing():
    """
    CASE A: Briefing missing at startup
    
    Expected flow:
    1. Load returns None
    2. Validate rejects it
    3. Safety gate fails
    4. Screener MUST run to generate briefing
    """
    logger.info("\n" + "="*70)
    logger.info("CASE A: BRIEFING MISSING AT STARTUP")
    logger.info("="*70)
    
    delete_briefing()
    
    # STEP 1: Load (should return None)
    logger.info("[STEP 1] Loading briefing...")
    briefing = load_briefing()
    logger.info(f"Result: {type(briefing).__name__} = {briefing is None}")
    assert briefing is None, "CASE A FAILED: load_briefing should return None"
    
    # STEP 2: Validate (should reject)
    logger.info("[STEP 2] Validating briefing...")
    is_valid, reason = validate_briefing(briefing)
    logger.info(f"Result: is_valid={is_valid}, reason={reason}")
    assert not is_valid, "CASE A FAILED: validate_briefing should reject None"
    
    # STEP 3: Safety gate (should fail)
    logger.info("[STEP 3] Safety gate check...")
    is_safe, reason = is_briefing_safe_for_trading(briefing)
    logger.info(f"Result: is_safe={is_safe}, reason={reason}")
    assert not is_safe, "CASE A FAILED: is_briefing_safe_for_trading should reject None"
    
    # STEP 4: Decision (screener MUST run)
    logger.info("[STEP 4] Decision: SCREENER MUST RUN")
    logger.info("✅ CASE A PASSED")
    return True

def test_case_b_empty_briefing():
    """
    CASE B: Briefing exists but is empty (no stocks)
    
    Expected flow:
    1. Load returns briefing
    2. Validate rejects it (empty)
    3. Safety gate fails
    4. Screener MUST run to populate briefing
    """
    logger.info("\n" + "="*70)
    logger.info("CASE B: BRIEFING EMPTY (NO STOCKS)")
    logger.info("="*70)
    
    create_empty_briefing()
    
    # STEP 1: Load (should succeed)
    logger.info("[STEP 1] Loading briefing...")
    briefing = load_briefing()
    logger.info(f"Result: {type(briefing).__name__} with {len(briefing.get('approved_stocks', []))} approved + {len(briefing.get('watchlist', []))} watchlist")
    assert briefing is not None, "CASE B FAILED: load_briefing should not return None"
    
    # STEP 2: Validate (should reject - empty)
    logger.info("[STEP 2] Validating briefing...")
    is_valid, reason = validate_briefing(briefing)
    logger.info(f"Result: is_valid={is_valid}, reason={reason}")
    assert not is_valid, f"CASE B FAILED: validate_briefing should reject empty briefing. Got: {reason}"
    assert "empty" in reason.lower(), f"CASE B FAILED: Reason should mention 'empty'. Got: {reason}"
    
    # STEP 3: Safety gate (should fail)
    logger.info("[STEP 3] Safety gate check...")
    is_safe, reason = is_briefing_safe_for_trading(briefing)
    logger.info(f"Result: is_safe={is_safe}, reason={reason}")
    assert not is_safe, "CASE B FAILED: is_briefing_safe_for_trading should reject empty briefing"
    
    # STEP 4: Decision (screener MUST run)
    logger.info("[STEP 4] Decision: SCREENER MUST RUN")
    logger.info("✅ CASE B PASSED")
    return True

def test_case_c_valid_briefing():
    """
    CASE C: Briefing valid (has stocks)
    
    Expected flow:
    1. Load returns briefing
    2. Validate accepts it
    3. Safety gate accepts it
    4. Trading CAN proceed
    """
    logger.info("\n" + "="*70)
    logger.info("CASE C: BRIEFING VALID (HAS STOCKS)")
    logger.info("="*70)
    
    create_valid_briefing()
    
    # STEP 1: Load (should succeed)
    logger.info("[STEP 1] Loading briefing...")
    briefing = load_briefing()
    logger.info(f"Result: {type(briefing).__name__} with {len(briefing.get('approved_stocks', []))} approved + {len(briefing.get('watchlist', []))} watchlist")
    assert briefing is not None, "CASE C FAILED: load_briefing should not return None"
    assert len(briefing.get('approved_stocks', [])) > 0 or len(briefing.get('watchlist', [])) > 0, "CASE C FAILED: Briefing should have stocks"
    
    # STEP 2: Validate (should accept)
    logger.info("[STEP 2] Validating briefing...")
    is_valid, reason = validate_briefing(briefing)
    logger.info(f"Result: is_valid={is_valid}, reason={reason}")
    assert is_valid, f"CASE C FAILED: validate_briefing should accept valid briefing. Got: {reason}"
    
    # STEP 3: Safety gate (should accept)
    logger.info("[STEP 3] Safety gate check...")
    is_safe, reason = is_briefing_safe_for_trading(briefing)
    logger.info(f"Result: is_safe={is_safe}, reason={reason}")
    assert is_safe, f"CASE C FAILED: is_briefing_safe_for_trading should accept valid briefing. Got: {reason}"
    
    # STEP 4: Decision (trading CAN proceed)
    logger.info("[STEP 4] Decision: TRADING CAN PROCEED")
    logger.info("✅ CASE C PASSED")
    return True

def test_case_d_test_briefing():
    """
    CASE D: TEST_* briefing present
    
    Expected flow:
    1. Load returns briefing
    2. Validate REJECTS it (TEST_* type)
    3. Safety gate fails
    4. Screener MUST run to regenerate
    """
    logger.info("\n" + "="*70)
    logger.info("CASE D: TEST_* BRIEFING PRESENT")
    logger.info("="*70)
    
    create_test_briefing()
    
    # STEP 1: Load (should succeed)
    logger.info("[STEP 1] Loading briefing...")
    briefing = load_briefing()
    logger.info(f"Result: {type(briefing).__name__} with session_type={briefing.get('session_type')}")
    assert briefing is not None, "CASE D FAILED: load_briefing should not return None"
    session_type = briefing.get('session_type', '')
    assert session_type.startswith('TEST'), f"CASE D FAILED: session_type should start with TEST. Got: {session_type}"
    
    # STEP 2: Validate (should REJECT test briefing)
    logger.info("[STEP 2] Validating briefing...")
    is_valid, reason = validate_briefing(briefing)
    logger.info(f"Result: is_valid={is_valid}, reason={reason}")
    assert not is_valid, f"CASE D FAILED: validate_briefing should reject TEST briefing. Got: is_valid={is_valid}"
    assert "test" in reason.lower(), f"CASE D FAILED: Reason should mention 'test'. Got: {reason}"
    
    # STEP 3: Safety gate (should fail)
    logger.info("[STEP 3] Safety gate check...")
    is_safe, reason = is_briefing_safe_for_trading(briefing)
    logger.info(f"Result: is_safe={is_safe}, reason={reason}")
    assert not is_safe, "CASE D FAILED: is_briefing_safe_for_trading should reject TEST briefing"
    
    # STEP 4: Decision (screener MUST run)
    logger.info("[STEP 4] Decision: SCREENER MUST RUN (to regenerate)")
    logger.info("✅ CASE D PASSED")
    return True

def test_placeholder_briefing_rejection():
    """
    BONUS: Placeholder briefing with do_not_use_for_trading flag
    
    Expected:
    - Validate should reject it
    - Safety gate should fail
    - Message should be clear about placeholder status
    """
    logger.info("\n" + "="*70)
    logger.info("BONUS: PLACEHOLDER BRIEFING REJECTION")
    logger.info("="*70)
    
    create_placeholder_briefing()
    
    # Load placeholder
    logger.info("[STEP 1] Loading placeholder briefing...")
    briefing = load_briefing()
    logger.info(f"Result: do_not_use_for_trading={briefing.get('do_not_use_for_trading')}")
    assert briefing.get('do_not_use_for_trading') is True, "BONUS FAILED: Placeholder should have marker"
    
    # Validate should reject
    logger.info("[STEP 2] Validating placeholder...")
    is_valid, reason = validate_briefing(briefing)
    logger.info(f"Result: is_valid={is_valid}, reason={reason}")
    assert not is_valid, "BONUS FAILED: validate_briefing should reject placeholder"
    assert "placeholder" in reason.lower(), f"BONUS FAILED: Reason should mention 'placeholder'. Got: {reason}"
    
    # Safety gate should fail
    logger.info("[STEP 3] Safety gate check...")
    is_safe, reason = is_briefing_safe_for_trading(briefing)
    logger.info(f"Result: is_safe={is_safe}")
    assert not is_safe, "BONUS FAILED: is_briefing_safe_for_trading should reject placeholder"
    
    logger.info("✅ BONUS PASSED: Placeholder briefing correctly rejected")
    return True

# ════════════════════════════════════════════════════════════
#   MAIN TEST RUNNER
# ════════════════════════════════════════════════════════════

def main():
    logger.info("\n" + "="*70)
    logger.info("BRIEFING LIFECYCLE HARDENING TEST SUITE")
    logger.info("="*70)
    
    backup_path = backup_briefing()
    
    results = {}
    tests = [
        ("Case A: Missing Briefing", test_case_a_missing_briefing),
        ("Case B: Empty Briefing", test_case_b_empty_briefing),
        ("Case C: Valid Briefing", test_case_c_valid_briefing),
        ("Case D: TEST Briefing", test_case_d_test_briefing),
        ("Bonus: Placeholder", test_placeholder_briefing_rejection),
    ]
    
    for test_name, test_fn in tests:
        try:
            result = test_fn()
            results[test_name] = "✅ PASSED"
        except AssertionError as e:
            results[test_name] = f"❌ FAILED: {str(e)}"
            logger.error(f"Test failed: {e}")
        except Exception as e:
            results[test_name] = f"❌ ERROR: {str(e)}"
            logger.error(f"Test error: {e}", exc_info=True)
    
    # Restore original briefing
    if backup_path:
        restore_briefing(backup_path)
    
    # Print summary
    logger.info("\n" + "="*70)
    logger.info("TEST SUMMARY")
    logger.info("="*70)
    for test_name, result in results.items():
        logger.info(f"{test_name}: {result}")
    
    # Overall result
    all_passed = all("✅ PASSED" in result for result in results.values())
    logger.info("="*70)
    if all_passed:
        logger.info("🎉 ALL TESTS PASSED - BRIEFING LIFECYCLE IS HARDENED")
        return 0
    else:
        logger.error("⚠️  SOME TESTS FAILED - REVIEW ABOVE")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
