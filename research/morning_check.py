"""
STRONG40 Morning Regime Checker
================================
Run this at 9:15-9:20 AM every day BEFORE starting the trading bot.

Logic (from backtesting research):
  - Fetch all 48 Nifty50 stocks' current open price
  - Compare with yesterday's close
  - If >= 40% of stocks have gapped up by >= 0.5%, TRADE TODAY
  - Otherwise, sit out and save capital

Result: +21.8% return in 60 days (vs +5.9% with no filter)
        WR: 59.8% | max_pos: 3 | Partial exit ON (RSI > 72)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime
from screener.morning_screener import NIFTY_50, _fetch_yahoo_history

STRONG_GAP_MIN_STOCKS_PCT = 0.40   # 40% of stocks must qualify
STRONG_GAP_PER_STOCK_PCT  = 0.005  # Each stock must gap up >= 0.5%

def check_strong40_regime():
    print("=" * 60)
    print(f"STRONG40 MORNING CHECK — {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}")
    print("=" * 60)
    print(f"Checking {len(NIFTY_50)} Nifty50 stocks...\n")

    qualifiers = []
    fails = []
    errors = []

    for sym in NIFTY_50:
        try:
            # Fetch last 2 days of daily data
            df = _fetch_yahoo_history(sym, period="5d", interval="1d")
            if df is None or df.empty or len(df) < 2:
                errors.append(sym); continue

            df.columns = [c.lower() for c in df.columns]
            prev_close = float(df["close"].iloc[-2])
            today_open = float(df["open"].iloc[-1])

            gap_pct = (today_open - prev_close) / prev_close

            if gap_pct >= STRONG_GAP_PER_STOCK_PCT:
                qualifiers.append((sym, gap_pct * 100))
            else:
                fails.append((sym, gap_pct * 100))
        except Exception as e:
            errors.append(sym)

    total = len(qualifiers) + len(fails)
    if total == 0:
        print("ERROR: No data fetched. Check internet/yfinance.")
        return False

    pct_strong = len(qualifiers) / total
    threshold_met = pct_strong >= STRONG_GAP_MIN_STOCKS_PCT

    print(f"Stocks with gap >= 0.5%:  {len(qualifiers)}/{total} = {pct_strong:.1%}")
    print(f"Required threshold:        >= {STRONG_GAP_MIN_STOCKS_PCT:.0%} ({int(STRONG_GAP_MIN_STOCKS_PCT*total)}+ stocks)")

    if qualifiers:
        top5 = sorted(qualifiers, key=lambda x: x[1], reverse=True)[:5]
        print(f"\nTop gappers: {', '.join(f'{s}({g:+.1f}%)' for s,g in top5)}")

    print()
    if threshold_met:
        print("=" * 60)
        print("  ✅  TRADE TODAY — STRONG40 regime CONFIRMED")
        print(f"  {len(qualifiers)} stocks gapped up >= 0.5%")
        print(f"  Strategy: BUY_STREAK_MOMENTUM_BREAKOUT")
        print(f"  Config:   max_pos=3 | Partial exit ON (RSI>72)")
        print("=" * 60)
    else:
        print("=" * 60)
        print("  ❌  SIT OUT TODAY — STRONG40 regime NOT met")
        print(f"  Only {len(qualifiers)} stocks gapped up >= 0.5%")
        print(f"  Need {int(STRONG_GAP_MIN_STOCKS_PCT*total)}+ stocks. Save capital.")
        print("=" * 60)

    if errors:
        print(f"\n  (Data unavailable for: {', '.join(errors[:5])}{'...' if len(errors)>5 else ''})")

    return threshold_met

if __name__ == "__main__":
    result = check_strong40_regime()
    sys.exit(0 if result else 1)
