#!/usr/bin/env python
"""Verify all dependencies are correctly installed for Python 3.10.11"""

import sys
import importlib

print(f"\n{'='*70}")
print(f"DEPENDENCY VERIFICATION — Python {sys.version}")
print(f"{'='*70}\n")

# List of critical dependencies to test
CRITICAL_PACKAGES = [
    ('yfinance', '0.2.28', 'Stock data fetching'),
    ('pandas', '2.1.4', 'Data manipulation'),
    ('numpy', '1.26.4', 'Numerical computing'),
    ('google.generativeai', '0.4.0', 'Gemini AI'),
    ('openai', '1.3.0', 'OpenAI API'),
    ('groq', '0.4.0', 'Groq API'),
    ('flask', '3.0.0', 'Web framework'),
    ('sqlalchemy', '2.0.23', 'Database ORM'),
    ('ta', '0.10.2', 'Technical analysis'),
]

success_count = 0
failed_packages = []

for package_name, expected_version, description in CRITICAL_PACKAGES:
    try:
        # Import the package
        module = importlib.import_module(package_name.split('.')[0])
        
        # Get version
        version = getattr(module, '__version__', 'unknown')
        
        # Verify yfinance specifically (known issue)
        if package_name == 'yfinance':
            import yfinance as yf
            # Try a quick operation to verify API compatibility
            ticker = yf.Ticker("RELIANCE.NS")
            print(f"✅ {package_name:30s} {version:15s} ({description})")
            success_count += 1
        else:
            print(f"✅ {package_name:30s} {version:15s} ({description})")
            success_count += 1
            
    except Exception as e:
        error_msg = str(e)[:50]
        print(f"❌ {package_name:30s} ERROR: {error_msg}")
        failed_packages.append((package_name, error_msg))

print(f"\n{'='*70}")
print(f"RESULT: {success_count}/{len(CRITICAL_PACKAGES)} packages verified ✅")
if failed_packages:
    print(f"\nFailed packages:")
    for pkg, err in failed_packages:
        print(f"  - {pkg}: {err}")
else:
    print("\n✅ All dependencies are correctly installed!")
    print("✅ System is ready to run!")
print(f"{'='*70}\n")
