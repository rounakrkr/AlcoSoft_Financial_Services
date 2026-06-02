#!/usr/bin/env python
"""Check current environment status"""
import sys
import subprocess

print("\n=== ENVIRONMENT STATUS ===\n")

# Get pip list
result = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
lines = result.stdout.split("\n")

packages_to_check = ["yfinance", "neo-api-client", "certifi", "websockets", "pandas", "numpy"]
print("INSTALLED VERSIONS:")
for line in lines:
    for pkg in packages_to_check:
        if pkg.lower() in line.lower():
            print(f"  {line.strip()}")

# Check for conflicts
result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
print("\nDEPENDENCY CONFLICTS:")
if "conflicts" in result.stdout.lower() or result.stdout.strip() == "":
    if result.stdout.strip():
        print(result.stdout[:500])
    else:
        print("  ✅ NO CONFLICTS")
else:
    print(result.stdout[:500])

print("\n=== END STATUS ===\n")
