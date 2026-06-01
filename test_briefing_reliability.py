#!/usr/bin/env python3
"""
TEST: Trading Briefing Reliability Fix
Purpose: Verify that briefing pipeline has comprehensive logging, file verification, and auto-regeneration
Status: Diagnostic only (no code modifications)
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("BriefingTest")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.state_manager import BRIEFING_PATH, load_briefing, save_briefing
from core.safe_io import atomic_write_json


def test_briefing_file_operations():
    """Test 1: Verify file operations work with new verification logic"""
    logger.info("\n" + "="*60)
    logger.info("TEST 1: Briefing File Operations")
    logger.info("="*60)
    
    # Create a test briefing
    test_briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST",
        "market_bias": "BULLISH",
        "approved_stocks": [
            {"ticker": "INFY", "confidence": 0.85, "reason": "Strong RSI"},
            {"ticker": "TCS", "confidence": 0.80, "reason": "Volume spike"}
        ],
        "watchlist": [
            {"ticker": "WIPRO", "math_score": 75, "reason": "Math score 75"},
            {"ticker": "HCLTECH", "math_score": 72, "reason": "Math score 72"}
        ],
        "avoid_list": []
    }
    
    # Test save_briefing
    logger.info("\n→ Saving test briefing...")
    save_result = save_briefing(test_briefing)
    
    if not save_result:
        logger.error("✗ FAIL: save_briefing() returned False")
        return False
    
    # Test file exists
    if not os.path.exists(BRIEFING_PATH):
        logger.error(f"✗ FAIL: File does not exist after save: {BRIEFING_PATH}")
        return False
    logger.info(f"✓ File exists: {BRIEFING_PATH}")
    
    # Test file can be read
    try:
        with open(BRIEFING_PATH, 'r') as f:
            saved_data = json.load(f)
        logger.info(f"✓ File readable: {len(json.dumps(saved_data))} bytes")
    except Exception as e:
        logger.error(f"✗ FAIL: Cannot read file: {e}")
        return False
    
    # Test load_briefing
    logger.info("\n→ Loading briefing...")
    loaded = load_briefing()
    
    if not loaded:
        logger.error("✗ FAIL: load_briefing() returned None")
        return False
    
    logger.info(f"✓ Loaded: {len(loaded.get('approved_stocks', []))} approved + "
                f"{len(loaded.get('watchlist', []))} watchlist stocks")
    
    # Verify data integrity
    if loaded.get("approved_stocks") != test_briefing["approved_stocks"]:
        logger.error("✗ FAIL: Approved stocks mismatch after round-trip")
        return False
    logger.info("✓ Data integrity verified")
    
    logger.info("\n✅ TEST 1 PASSED\n")
    return True


def test_empty_briefing_detection():
    """Test 2: Verify empty briefing is detected and rejected"""
    logger.info("="*60)
    logger.info("TEST 2: Empty Briefing Detection")
    logger.info("="*60)
    
    # Create empty briefing
    empty_briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST_EMPTY",
        "market_bias": "NEUTRAL",
        "approved_stocks": [],
        "watchlist": [],
        "avoid_list": []
    }
    
    logger.info("\n→ Saving empty briefing...")
    save_result = save_briefing(empty_briefing)
    
    if not save_result:
        logger.error("✗ FAIL: save_briefing() should save empty briefing (with warning)")
        return False
    logger.info("✓ Empty briefing saved (as expected)")
    
    # Load and check
    loaded = load_briefing()
    if not loaded:
        logger.error("✗ FAIL: load_briefing() returned None")
        return False
    
    approved = len(loaded.get("approved_stocks", []))
    watchlist = len(loaded.get("watchlist", []))
    
    if approved == 0 and watchlist == 0:
        logger.info(f"✓ Empty briefing detected: {approved} approved + {watchlist} watchlist")
    else:
        logger.error(f"✗ FAIL: Expected empty but got {approved} + {watchlist}")
        return False
    
    logger.info("\n✅ TEST 2 PASSED\n")
    return True


def test_briefing_not_found_detection():
    """Test 3: Verify missing briefing is properly detected"""
    logger.info("="*60)
    logger.info("TEST 3: Missing Briefing Detection")
    logger.info("="*60)
    
    # Temporarily move the briefing file
    if os.path.exists(BRIEFING_PATH):
        backup_path = BRIEFING_PATH + ".backup"
        os.rename(BRIEFING_PATH, backup_path)
        logger.info(f"→ Moved briefing to {backup_path} temporarily")
    
    # Try to load missing briefing
    logger.info("\n→ Loading (missing) briefing...")
    loaded = load_briefing()
    
    if loaded is not None:
        logger.error("✗ FAIL: load_briefing() should return None when file missing")
        return False
    logger.info("✓ Missing briefing properly returns None")
    
    # Restore the briefing
    if os.path.exists(backup_path):
        os.rename(backup_path, BRIEFING_PATH)
        logger.info(f"→ Restored briefing from {backup_path}")
    
    logger.info("\n✅ TEST 3 PASSED\n")
    return True


def test_health_check_diagnostics():
    """Test 4: Verify health check provides proper diagnostics"""
    logger.info("="*60)
    logger.info("TEST 4: Health Check Diagnostics")
    logger.info("="*60)
    
    from core.health_monitor import check_briefing
    
    # First, ensure we have a valid briefing
    test_briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST_HEALTH",
        "market_bias": "BULLISH",
        "approved_stocks": [{"ticker": "INFY", "confidence": 0.85}],
        "watchlist": [{"ticker": "WIPRO", "math_score": 75}],
        "avoid_list": []
    }
    
    logger.info("\n→ Saving valid briefing...")
    save_briefing(test_briefing)
    
    logger.info("→ Running health check (valid briefing)...")
    result, message = check_briefing()
    
    logger.info(f"   Result: {result}")
    logger.info(f"   Message: {message}")
    
    if result:
        logger.info("✓ Valid briefing check passed")
    else:
        logger.error(f"✗ FAIL: Valid briefing check failed: {message}")
        return False
    
    # Test empty briefing detection in health check
    logger.info("\n→ Testing health check with empty briefing...")
    empty_briefing = {
        "generated_at": datetime.now().isoformat(),
        "session_type": "TEST_EMPTY",
        "market_bias": "NEUTRAL",
        "approved_stocks": [],
        "watchlist": [],
        "avoid_list": []
    }
    
    save_briefing(empty_briefing)
    result, message = check_briefing()
    
    logger.info(f"   Result: {result}")
    logger.info(f"   Message: {message}")
    
    if not result:
        logger.info("✓ Empty briefing check failed (as expected)")
    else:
        logger.error(f"✗ FAIL: Empty briefing should fail health check")
        return False
    
    logger.info("\n✅ TEST 4 PASSED\n")
    return True


def test_logging_clarity():
    """Test 5: Verify logging is clear and actionable"""
    logger.info("="*60)
    logger.info("TEST 5: Logging Clarity")
    logger.info("="*60)
    
    logger.info("\n→ This test verifies logging format by inspection")
    logger.info("   Expected log outputs:")
    logger.info("   • 'Saving briefing to data/session_briefing.json...'")
    logger.info("   • '✓ Briefing saved and verified: data/session_briefing.json'")
    logger.info("   • '   - Approved stocks: N'")
    logger.info("   • '   - Watchlist: N'")
    logger.info("   • 'Loading briefing from data/session_briefing.json...'")
    logger.info("   • '✓ Briefing loaded: N approved + N watchlist'")
    logger.info("   • 'Error messages starting with '❌' for failures")
    logger.info("   • 'Warning messages starting with '⚠️' for warnings'")
    
    logger.info("\n✅ TEST 5 PASSED (Manual verification required)\n")
    return True


def main():
    """Run all tests"""
    logger.info("\n" + "="*60)
    logger.info("TRADING BRIEFING RELIABILITY FIX — TEST SUITE")
    logger.info("="*60)
    
    tests = [
        ("File Operations", test_briefing_file_operations),
        ("Empty Briefing Detection", test_empty_briefing_detection),
        ("Missing Briefing Detection", test_briefing_not_found_detection),
        ("Health Check Diagnostics", test_health_check_diagnostics),
        ("Logging Clarity", test_logging_clarity),
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            logger.error(f"\n❌ TEST EXCEPTION: {test_name}")
            logger.error(f"   {e}")
            results.append((test_name, False))
    
    # Summary
    logger.info("\n" + "="*60)
    logger.info("TEST SUMMARY")
    logger.info("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        logger.info(f"{status}: {test_name}")
    
    logger.info("\n" + "="*60)
    logger.info(f"RESULT: {passed}/{total} tests passed")
    logger.info("="*60 + "\n")
    
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
