"""
MARKET ANALYSIS REPORT — Last 2 Months
=========================================
1. All 48 stocks combined:
   - % days closed higher vs lower (daily basis)
   - Profit/loss range distribution
2. Nifty50 index:
   - Days up vs down, points, %
   - Highest gain/loss day
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import yfinance as yf
import logging, warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore")

from research.build_cache import load_cache

stock_dfs = load_cache()
print(f"{len(stock_dfs)} stocks loaded from cache.\n")

# ── SECTION 1: Per-stock daily analysis ────────────────────────
print("="*90)
print("SECTION 1: ALL 48 STOCKS — Daily Close vs Open (each stock, each day)")
print("="*90)

all_daily_changes = []   # (sym, date, open, close, pct_change)

for sym, df in stock_dfs.items():
    for d, grp in sorted(df.groupby(df.index.date)):
        if grp.empty: continue
        day_open  = float(grp["open"].iloc[0])
        day_close = float(grp["close"].iloc[-1])
        if day_open <= 0: continue
        pct = (day_close - day_open) / day_open * 100
        all_daily_changes.append({
            "sym": sym, "date": d,
            "open": day_open, "close": day_close, "pct": pct
        })

df_all = pd.DataFrame(all_daily_changes)
total  = len(df_all)
up_days   = df_all[df_all["pct"] > 0]
down_days = df_all[df_all["pct"] < 0]
flat_days = df_all[df_all["pct"] == 0]

print(f"\nTotal stock-days analyzed: {total}")
print(f"  Closed UP   (close > open): {len(up_days):5d}  ({len(up_days)/total*100:.1f}%)")
print(f"  Closed DOWN (close < open): {len(down_days):5d}  ({len(down_days)/total*100:.1f}%)")
print(f"  Flat        (close = open): {len(flat_days):5d}  ({len(flat_days)/total*100:.1f}%)")

# Profit distribution (up days)
print(f"\n--- PROFIT DISTRIBUTION (close > open days) ---")
up_pct = up_days["pct"].values
buckets_up = [
    (0.0, 0.2, "0 - 0.2%  (tiny gains)"),
    (0.2, 0.5, "0.2 - 0.5%"),
    (0.5, 1.0, "0.5 - 1.0%"),
    (1.0, 2.0, "1.0 - 2.0%"),
    (2.0, 3.0, "2.0 - 3.0%"),
    (3.0, 5.0, "3.0 - 5.0%"),
    (5.0, 100, ">5.0%  (big movers)"),
]
for lo, hi, label in buckets_up:
    cnt = np.sum((up_pct >= lo) & (up_pct < hi))
    bar = "#" * min(40, cnt * 40 // max(len(up_pct), 1))
    print(f"  {label:30s}: {cnt:5d} ({cnt/total*100:5.1f}%)  {bar}")
print(f"  Mean profit on up days  : +{np.mean(up_pct):.3f}%")
print(f"  Median profit on up days: +{np.median(up_pct):.3f}%")
print(f"  Best single day gain    : +{np.max(up_pct):.2f}%  ({up_days.loc[up_days['pct'].idxmax(), 'sym']} on {up_days.loc[up_days['pct'].idxmax(), 'date']})")

# Loss distribution (down days)
print(f"\n--- LOSS DISTRIBUTION (close < open days) ---")
dn_pct = np.abs(down_days["pct"].values)
buckets_dn = [
    (0.0, 0.2, "0 - 0.2%  (tiny losses)"),
    (0.2, 0.5, "0.2 - 0.5%"),
    (0.5, 1.0, "0.5 - 1.0%"),
    (1.0, 2.0, "1.0 - 2.0%"),
    (2.0, 3.0, "2.0 - 3.0%"),
    (3.0, 5.0, "3.0 - 5.0%"),
    (5.0, 100, ">5.0%  (big drops)"),
]
for lo, hi, label in buckets_dn:
    cnt = np.sum((dn_pct >= lo) & (dn_pct < hi))
    bar = "#" * min(40, cnt * 40 // max(len(dn_pct), 1))
    print(f"  {label:30s}: {cnt:5d} ({cnt/total*100:5.1f}%)  {bar}")
print(f"  Mean loss on down days  : -{np.mean(dn_pct):.3f}%")
print(f"  Median loss on down days: -{np.median(dn_pct):.3f}%")
worst_idx = down_days["pct"].idxmin()
print(f"  Worst single day loss   : {down_days.loc[worst_idx, 'pct']:.2f}%  ({down_days.loc[worst_idx, 'sym']} on {down_days.loc[worst_idx, 'date']})")

# ── SECTION 2: Per-stock summary ───────────────────────────────
print(f"\n{'='*90}")
print("SECTION 2: PER-STOCK SUMMARY — Win rate, avg gain, avg loss")
print(f"{'='*90}")
print(f"\n  {'Stock':<14} {'Days':>5} {'Up%':>7} {'AvgGain':>9} {'AvgLoss':>9} {'Best Day':>9} {'Worst Day':>10}")
print(f"  {'-'*70}")

stock_summary = []
for sym in sorted(stock_dfs.keys()):
    sdf = df_all[df_all["sym"] == sym]
    if sdf.empty: continue
    up   = sdf[sdf["pct"] > 0]["pct"]
    dn   = sdf[sdf["pct"] < 0]["pct"]
    win_rate = len(up) / len(sdf) * 100
    avg_gain = up.mean() if len(up) > 0 else 0
    avg_loss = dn.mean() if len(dn) > 0 else 0
    best  = sdf["pct"].max()
    worst = sdf["pct"].min()
    stock_summary.append({"sym": sym, "days": len(sdf), "wr": win_rate,
                          "avg_gain": avg_gain, "avg_loss": avg_loss,
                          "best": best, "worst": worst})
    print(f"  {sym:<14} {len(sdf):>5} {win_rate:>6.1f}% {avg_gain:>+9.2f}% {avg_loss:>+9.2f}% "
          f"{best:>+9.2f}% {worst:>+10.2f}%")

# Overall stats
ss = pd.DataFrame(stock_summary)
print(f"\n  OVERALL AVERAGES:")
print(f"  Avg stock win rate   : {ss['wr'].mean():.1f}%")
print(f"  Avg gain on up days  : +{ss['avg_gain'].mean():.3f}%")
print(f"  Avg loss on down days: {ss['avg_loss'].mean():.3f}%")
print(f"  Most bullish stock   : {ss.loc[ss['wr'].idxmax(), 'sym']} (WR={ss['wr'].max():.1f}%)")
print(f"  Most bearish stock   : {ss.loc[ss['wr'].idxmin(), 'sym']} (WR={ss['wr'].min():.1f}%)")

# ── SECTION 3: DAILY MARKET DIRECTION ─────────────────────────
print(f"\n{'='*90}")
print("SECTION 3: DAILY MARKET DIRECTION — How many stocks moved UP vs DOWN each day?")
print(f"{'='*90}")

all_dates = sorted(df_all["date"].unique())
daily_market = []
for d in all_dates:
    day = df_all[df_all["date"] == d]
    up_c  = len(day[day["pct"] > 0])
    dn_c  = len(day[day["pct"] < 0])
    total_c = len(day)
    avg_chg = day["pct"].mean()
    daily_market.append({"date": d, "up": up_c, "down": dn_c,
                          "total": total_c, "up_pct": up_c/total_c*100,
                          "avg_chg": avg_chg})

dm = pd.DataFrame(daily_market)
bull_days = dm[dm["up_pct"] >= 60]
bear_days = dm[dm["up_pct"] <  40]
mixed_days = dm[(dm["up_pct"] >= 40) & (dm["up_pct"] < 60)]

print(f"\n  Total trading days: {len(dm)}")
print(f"  BULL days (>60% stocks up):   {len(bull_days):3d} ({len(bull_days)/len(dm)*100:.1f}%)")
print(f"  BEAR days (<40% stocks up):   {len(bear_days):3d} ({len(bear_days)/len(dm)*100:.1f}%)")
print(f"  MIXED days (40-60% stocks up):{len(mixed_days):3d} ({len(mixed_days)/len(dm)*100:.1f}%)")

print(f"\n  Date        Stocks Up   Stocks Dn   Up%    Avg Chg%   Market")
print(f"  {'-'*70}")
for _, row in dm.iterrows():
    direction = "BULL" if row["up_pct"] >= 60 else ("BEAR" if row["up_pct"] < 40 else "MIXED")
    marker = "^^" if row["up_pct"] >= 60 else ("vv" if row["up_pct"] < 40 else "--")
    print(f"  {str(row['date']):<12} {row['up']:>8}    {row['down']:>9}   "
          f"{row['up_pct']:>5.1f}%   {row['avg_chg']:>+7.3f}%   {marker} {direction}")

# ── SECTION 4: NIFTY50 INDEX ──────────────────────────────────
print(f"\n{'='*90}")
print("SECTION 4: NIFTY50 INDEX — Last 2 months")
print(f"{'='*90}")

try:
    nifty = yf.download("^NSEI", period="62d", interval="1d", progress=False)
    if nifty.empty:
        raise ValueError("No data")

    # Flatten if MultiIndex
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = nifty.columns.get_level_values(0)

    nifty = nifty.dropna(subset=["Open", "Close"])
    nifty["pct"] = (nifty["Close"] - nifty["Open"]) / nifty["Open"] * 100
    nifty["pts"] = nifty["Close"] - nifty["Open"]

    up_nifty   = nifty[nifty["pct"] > 0]
    down_nifty = nifty[nifty["pct"] < 0]

    print(f"\n  Total trading days: {len(nifty)}")
    print(f"  Days closed UP    : {len(up_nifty)} ({len(up_nifty)/len(nifty)*100:.1f}%)")
    print(f"  Days closed DOWN  : {len(down_nifty)} ({len(down_nifty)/len(nifty)*100:.1f}%)")

    print(f"\n  UP days stats:")
    print(f"    Avg gain     : +{up_nifty['pct'].mean():.3f}%  (+{up_nifty['pts'].mean():.0f} pts)")
    print(f"    Median gain  : +{up_nifty['pct'].median():.3f}%  (+{up_nifty['pts'].median():.0f} pts)")
    print(f"    Total pts gained: +{up_nifty['pts'].sum():.0f} pts")

    print(f"\n  DOWN days stats:")
    print(f"    Avg loss     : {down_nifty['pct'].mean():.3f}%  ({down_nifty['pts'].mean():.0f} pts)")
    print(f"    Median loss  : {down_nifty['pct'].median():.3f}%  ({down_nifty['pts'].median():.0f} pts)")
    print(f"    Total pts lost  : {down_nifty['pts'].sum():.0f} pts")

    best_day  = nifty["pct"].idxmax()
    worst_day = nifty["pct"].idxmin()
    print(f"\n  BEST day  : {str(best_day.date())} | +{nifty.loc[best_day,'pct']:.2f}% | +{nifty.loc[best_day,'pts']:.0f} pts")
    print(f"  WORST day : {str(worst_day.date())} | {nifty.loc[worst_day,'pct']:.2f}% | {nifty.loc[worst_day,'pts']:.0f} pts")

    print(f"\n  Overall 2-month period:")
    first_open = float(nifty["Open"].iloc[0])
    last_close = float(nifty["Close"].iloc[-1])
    total_pct  = (last_close - first_open) / first_open * 100
    total_pts  = last_close - first_open
    print(f"    Start (open): {first_open:.0f}")
    print(f"    End (close) : {last_close:.0f}")
    print(f"    Net change  : {total_pct:+.2f}%  ({total_pts:+.0f} pts)")

    print(f"\n  NIFTY Day-by-Day:")
    print(f"  {'Date':<12} {'Open':>8} {'Close':>8} {'Chg Pts':>9} {'Chg%':>8}  Direction")
    print(f"  {'-'*60}")
    for idx, row in nifty.iterrows():
        d_str  = str(idx.date())
        arrow  = "UP  ^" if row["pct"] > 0 else "DOWN v"
        print(f"  {d_str:<12} {row['Open']:>8.0f} {row['Close']:>8.0f} "
              f"{row['pts']:>+9.0f} {row['pct']:>+7.2f}%  {arrow}")

except Exception as e:
    print(f"  Could not fetch Nifty data: {e}")

print(f"\nREPORT COMPLETE.")
