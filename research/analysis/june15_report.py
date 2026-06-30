import sys
from collections import defaultdict

def analyze_june_15():
    tearsheet_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\dual_engine_tearsheet.txt"
    
    long_trades = []
    short_trades = []
    
    try:
        with open(tearsheet_path, "r", encoding="utf-8") as f:
            for line in f:
                if "|LONG|" in line and "2026-06-15" in line:
                    long_trades.append(line.strip().split("|"))
                elif "|SHORT|" in line and "2026-06-15" in line:
                    short_trades.append(line.strip().split("|"))
    except FileNotFoundError:
        print("Tearsheet not found.")
        return

    all_trades = long_trades + short_trades
    
    if not all_trades:
        print("No trades found on 15th June 2026.")
        return

    total_gross = 0.0
    total_stt = 0.0
    wins = 0
    losses = 0

    print("="*80)
    print("DETAILED REPORT FOR 15TH JUNE 2026")
    print("="*80)
    print(f"Total Trades Taken: {len(all_trades)}")
    
    print("\n--- LONG TRADES ---")
    if not long_trades: print("None")
    for t in long_trades:
        sym, _, entry, ext, qty, ep, xp, reason, net_pnl = t
        net_pnl = float(net_pnl)
        ep = float(ep)
        xp = float(xp)
        qty = int(qty)
        turnover = xp * qty
        stt = turnover * 0.00035
        gross = (xp - ep) * qty
        total_gross += gross
        total_stt += stt
        if net_pnl > 0: wins += 1
        else: losses += 1
        print(f"[{entry.split()[1]} -> {ext.split()[1]}] {sym} | Entry: {ep:.2f} | Exit: {xp:.2f} ({reason}) | PnL: Rs.{net_pnl:.2f}")

    print("\n--- SHORT TRADES ---")
    if not short_trades: print("None")
    for t in short_trades:
        sym, _, entry, ext, qty, ep, xp, reason, net_pnl = t
        net_pnl = float(net_pnl)
        ep = float(ep)
        xp = float(xp)
        qty = int(qty)
        turnover = ep * qty
        stt = turnover * 0.00035
        gross = (ep - xp) * qty
        total_gross += gross
        total_stt += stt
        if net_pnl > 0: wins += 1
        else: losses += 1
        print(f"[{entry.split()[1]} -> {ext.split()[1]}] {sym} | Entry: {ep:.2f} | Exit: {xp:.2f} ({reason}) | PnL: Rs.{net_pnl:.2f}")

    total_net = total_gross - total_stt
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"Win Rate:   {(wins/len(all_trades))*100:.2f}% ({wins} W / {losses} L)")
    print(f"Gross PnL:  Rs.{total_gross:.2f}")
    print(f"STT Tax:    Rs.{-total_stt:.2f}")
    print(f"Net PnL:    Rs.{total_net:.2f}")

if __name__ == "__main__":
    analyze_june_15()
