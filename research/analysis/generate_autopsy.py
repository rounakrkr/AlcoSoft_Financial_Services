import sys
from collections import defaultdict
import datetime

def generate_autopsy():
    tearsheet_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\dual_engine_tearsheet.txt"
    
    trades = []
    
    try:
        with open(tearsheet_path, "r", encoding="utf-8") as f:
            for line in f:
                if "| LONG   |" in line or "| SHORT  |" in line:
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 9:
                        sym, direction, entry, ext, qty, ep, xp, reason, net_pnl = parts[:9]
                        trades.append({
                            "direction": direction,
                            "entry": entry,
                            "reason": reason,
                            "net_pnl": float(net_pnl)
                        })
    except FileNotFoundError:
        print("Tearsheet not found.")
        return

    # Group by date
    days_data = defaultdict(lambda: {"direction": None, "pnl": 0.0, "trades": []})
    
    for t in trades:
        date_str = t["entry"].split(" ")[0]
        days_data[date_str]["pnl"] += t["net_pnl"]
        days_data[date_str]["trades"].append(t)
        # Assuming all trades in a day have the same direction for Dual Engine
        days_data[date_str]["direction"] = t["direction"]

    bull_profit_days = []
    bull_loss_days = []
    bear_profit_days = []
    bear_loss_days = []

    for date_str, data in days_data.items():
        if data["direction"] == "LONG":
            if data["pnl"] > 0:
                bull_profit_days.append(data["trades"])
            else:
                bull_loss_days.append(data["trades"])
        elif data["direction"] == "SHORT":
            if data["pnl"] > 0:
                bear_profit_days.append(data["trades"])
            else:
                bear_loss_days.append(data["trades"])

    print("# 🔬 ALCOSOFT DAY & REGIME AUTOPSY (PATH C - 1 SLOT)")
    print("> [!NOTE]")
    print("> **Analysis Focus:** Kitne Profit/Loss days Bull vs Bear the, aur un dino mein kis exit reason ne profits ya losses diye.")
    print("\n## 📊 1. Day Distribution Summary")
    print(f"- **Total Active Trading Days:** {len(days_data)}")
    total_profit_days = len(bull_profit_days) + len(bear_profit_days)
    total_loss_days = len(bull_loss_days) + len(bear_loss_days)
    print(f"- **Total Profit Days (Net Green):** {total_profit_days}")
    print(f"  - 🟢 Bull Profit Days: {len(bull_profit_days)}")
    print(f"  - 🔴 Bear Profit Days: {len(bear_profit_days)}")
    print(f"- **Total Loss Days (Net Red):** {total_loss_days}")
    print(f"  - 🟢 Bull Loss Days: {len(bull_loss_days)}")
    print(f"  - 🔴 Bear Loss Days: {len(bear_loss_days)}")

    def print_table(trades_list_of_lists, title):
        print(f"\n### {title}")
        print(f"**Total Days in this category:** {len(trades_list_of_lists)}\n")
        print("| Exit Reason | Total Wins 🏆 | Total Losses 💔 | PnL from Wins | PnL from Losses |")
        print("|-------------|--------------|-----------------|---------------|-----------------|")
        
        reason_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "win_pnl": 0.0, "loss_pnl": 0.0})
        
        for daily_trades in trades_list_of_lists:
            for t in daily_trades:
                r = t["reason"]
                p = t["net_pnl"]
                if p > 0:
                    reason_stats[r]["wins"] += 1
                    reason_stats[r]["win_pnl"] += p
                else:
                    reason_stats[r]["losses"] += 1
                    reason_stats[r]["loss_pnl"] += p

        for r, s in reason_stats.items():
            print(f"| **{r}** | {s['wins']} | {s['losses']} | ₹{s['win_pnl']:.2f} | ₹{s['loss_pnl']:.2f} |")

    print("\n---")
    print_table(bull_loss_days, "📉 LOSS DAYS WHICH WERE BULL DAYS (🟢)")
    print_table(bear_loss_days, "📉 LOSS DAYS WHICH WERE BEAR DAYS (🔴)")
    print("\n---")
    print_table(bull_profit_days, "📈 PROFIT DAYS WHICH WERE BULL DAYS (🟢)")
    print_table(bear_profit_days, "📈 PROFIT DAYS WHICH WERE BEAR DAYS (🔴)")

if __name__ == "__main__":
    generate_autopsy()
