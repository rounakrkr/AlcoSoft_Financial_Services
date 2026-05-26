import json
import os

os.chdir(r"c:\Extra Programs\Files\AlcoSoft_Financial_Services")

path = "data/session_briefing.json"
print(f"File exists: {os.path.exists(path)}")

if os.path.exists(path):
    b = json.load(open(path))
    print(f"Briefing loaded successfully")
    print(f"Keys: {list(b.keys())}")
    print(f"Approved stocks: {len(b.get('approved_stocks', []))}")
    print(f"Watchlist: {len(b.get('watchlist', []))}")
    
    # Check if any stocks in briefing
    all_stocks = [s.get('ticker') for s in b.get('approved_stocks', [])]
    all_stocks += [s.get('ticker') for s in b.get('watchlist', [])]
    print(f"All stocks: {all_stocks}")
else:
    print("Briefing file not found!")
