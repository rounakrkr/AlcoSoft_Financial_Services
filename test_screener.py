#!/usr/bin/env python
"""Test screener for all issues"""
import sys
import traceback

print("\n" + "="*80)
print("SCREENER DIAGNOSTIC TEST")
print("="*80 + "\n")

try:
    print("[1/3] Testing imports...")
    from screener.morning_screener import run_morning_screener
    print("✅ Imports successful\n")
    
    print("[2/3] Running screener...")
    result = run_morning_screener()
    print(f"\n[3/3] Result: {result}")
    
    if result:
        print("\n✅ SCREENER EXECUTED SUCCESSFULLY!")
    else:
        print("\n⚠️  SCREENER FAILED - Check logs above")
        
except Exception as e:
    print(f"\n❌ ERROR OCCURRED:")
    print(f"   {type(e).__name__}: {str(e)}")
    print("\nFull traceback:")
    traceback.print_exc()

print("\n" + "="*80 + "\n")
