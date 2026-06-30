import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from research.build_cache import load_cache

def analyze_gaps():
    print("Loading stocks...")
    stock_dfs = load_cache()
    stock_day_data = {}
    all_dates_set = set()
    
    for sym, df in stock_dfs.items():
        by_day = {}
        prev_close = None
        for d, grp in sorted(df.groupby(df.index.date)):
            all_dates_set.add(d)
            gap_pct = (float(grp["open"].iloc[0]) - prev_close) / prev_close if prev_close else 0.0
            c1_high = float(grp["high"].iloc[0])
            c1_low = float(grp["low"].iloc[0])
            
            c2_high = float(grp["high"].iloc[1]) if len(grp) > 1 else c1_high
            c2_low = float(grp["low"].iloc[1]) if len(grp) > 1 else c1_low
            
            by_day[d] = {
                "prev_close": prev_close,
                "gap_pct": gap_pct,
                "broke_high": c2_high > c1_high,
                "broke_low": c2_low < c1_low
            }
            prev_close = float(grp["close"].iloc[-1])
        stock_day_data[sym] = by_day

    print("Analyzing gap continuations...")
    results = []

    for threshold in [0.004, 0.005, 0.006, 0.007]:
        gap_up_regime_days = set()
        gap_down_regime_days = set()
        
        # Determine regime days
        for d in all_dates_set:
            stocks_with_data = [(sym, stock_day_data[sym][d])
                                for sym in stock_day_data if d in stock_day_data[sym]
                                and stock_day_data[sym][d]["prev_close"]]
            if not stocks_with_data: continue
            
            up_gaps = sum(1 for _, sd in stocks_with_data if sd["gap_pct"] >= threshold)
            down_gaps = sum(1 for _, sd in stocks_with_data if sd["gap_pct"] <= -threshold)
            
            if up_gaps / len(stocks_with_data) >= 0.40: gap_up_regime_days.add(d)
            if down_gaps / len(stocks_with_data) >= 0.40: gap_down_regime_days.add(d)
                
        # Now analyze continuation
        up_stocks_total = 0
        up_stocks_continued = 0
        
        for d in gap_up_regime_days:
            for sym, by_day in stock_day_data.items():
                if d in by_day and by_day[d]["prev_close"]:
                    if by_day[d]["gap_pct"] >= threshold:
                        up_stocks_total += 1
                        if by_day[d]["broke_high"]: up_stocks_continued += 1
                        
        down_stocks_total = 0
        down_stocks_continued = 0
        
        for d in gap_down_regime_days:
            for sym, by_day in stock_day_data.items():
                if d in by_day and by_day[d]["prev_close"]:
                    if by_day[d]["gap_pct"] <= -threshold:
                        down_stocks_total += 1
                        if by_day[d]["broke_low"]: down_stocks_continued += 1
                        
        results.append({
            "threshold": threshold,
            "gap_up_days": len(gap_up_regime_days),
            "up_total": up_stocks_total,
            "up_cont": up_stocks_continued,
            "up_pct": (up_stocks_continued / max(1, up_stocks_total)) * 100,
            
            "gap_down_days": len(gap_down_regime_days),
            "down_total": down_stocks_total,
            "down_cont": down_stocks_continued,
            "down_pct": (down_stocks_continued / max(1, down_stocks_total)) * 100
        })

    print("\n=== GAP CONTINUATION REPORT (2ND CANDLE BREAK) ===")
    for r in results:
        print(f"Threshold: {r['threshold']*100:.1f}%")
        print(f"  Gap UP 40% Days  : {r['gap_up_days']:2d} | Stocks that gapped UP: {r['up_total']:4d} | Broke 1st candle HIGH: {r['up_cont']:4d} ({r['up_pct']:.1f}%)")
        print(f"  Gap DOWN 40% Days: {r['gap_down_days']:2d} | Stocks that gapped DOWN: {r['down_total']:4d} | Broke 1st candle LOW: {r['down_cont']:4d} ({r['down_pct']:.1f}%)")
        print()

if __name__ == "__main__":
    analyze_gaps()
