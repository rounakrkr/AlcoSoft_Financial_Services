import os
import pandas as pd
from typing import List, Dict

def generate_report(trades: List[Dict], initial_capital: float, output_dir: str = "research/results"):
    os.makedirs(output_dir, exist_ok=True)
    
    if not trades:
        print("No trades generated during backtest.")
        return

    # Convert trades to DataFrame
    df_trades = pd.DataFrame(trades)
    
    # 1. Summary Calculation
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0])
    losing_trades = len(df_trades[df_trades["pnl"] <= 0])
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
    
    total_pnl = df_trades["pnl"].sum()
    final_capital = initial_capital + total_pnl
    total_return_pct = (total_pnl / initial_capital) * 100
    
    gross_profit = df_trades[df_trades["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(df_trades[df_trades["pnl"] < 0]["pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    
    avg_win = df_trades[df_trades["pnl"] > 0]["pnl"].mean() if winning_trades > 0 else 0
    avg_loss = df_trades[df_trades["pnl"] < 0]["pnl"].mean() if losing_trades > 0 else 0
    
    largest_win = df_trades["pnl"].max() if total_trades > 0 else 0
    largest_loss = df_trades["pnl"].min() if total_trades > 0 else 0
    
    # Max Drawdown
    df_trades["cumulative_pnl"] = df_trades["pnl"].cumsum()
    df_trades["peak_pnl"] = df_trades["cumulative_pnl"].cummax()
    df_trades["drawdown"] = df_trades["cumulative_pnl"] - df_trades["peak_pnl"]
    max_drawdown = df_trades["drawdown"].min()
    
    buy_strategy = df_trades["buy_strategy"].iloc[0] if "buy_strategy" in df_trades.columns else "N/A"
    sell_strategy = df_trades["sell_strategy"].iloc[0] if "sell_strategy" in df_trades.columns else "N/A"

    # 2. Summary.txt
    summary_path = os.path.join(output_dir, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("=== BACKTEST REPORT ===\n\n")
        f.write(f"BUY Strategy Used: {buy_strategy}\n")
        f.write(f"SELL Strategy Used: {sell_strategy}\n\n")
        
        f.write(f"Initial Capital: ₹{initial_capital:,.2f}\n")
        f.write(f"Final Capital:   ₹{final_capital:,.2f}\n")
        f.write(f"Total Return %:  {total_return_pct:.2f}%\n\n")
        
        f.write(f"Total Trades:    {total_trades}\n")
        f.write(f"Winning Trades:  {winning_trades}\n")
        f.write(f"Losing Trades:   {losing_trades}\n")
        f.write(f"Win Rate %:      {win_rate:.2f}%\n")
        f.write(f"Profit Factor:   {profit_factor:.2f}\n\n")
        
        f.write(f"Average Win:     ₹{avg_win:,.2f}\n")
        f.write(f"Average Loss:    ₹{avg_loss:,.2f}\n")
        f.write(f"Largest Win:     ₹{largest_win:,.2f}\n")
        f.write(f"Largest Loss:    ₹{largest_loss:,.2f}\n")
        f.write(f"Max Drawdown:    ₹{max_drawdown:,.2f}\n\n")
        
        # Stock-wise Aggregation for Best/Worst Performers
        stock_group = df_trades.groupby("stock")["pnl"].sum().reset_index()
        stock_group = stock_group.sort_values(by="pnl", ascending=False)
        
        f.write("=== BEST PERFORMERS (Top 10) ===\n")
        for idx, row in stock_group.head(10).iterrows():
            f.write(f"{row['stock']:<15} : ₹{row['pnl']:,.2f}\n")
            
        f.write("\n=== WORST PERFORMERS (Bottom 10) ===\n")
        for idx, row in stock_group.tail(10).iterrows():
            f.write(f"{row['stock']:<15} : ₹{row['pnl']:,.2f}\n")

    # 3. Trade Log
    log_columns = [
        "entry_time", "exit_time", "stock", "quantity", 
        "entry_price", "exit_price", "pnl", "exit_reason"
    ]
    df_log = df_trades[log_columns]
    df_log.to_csv(os.path.join(output_dir, "trade_log.csv"), index=False)

    # 4. Stock-wise Breakdown
    stock_stats = []
    for stock, group in df_trades.groupby("stock"):
        wins = len(group[group["pnl"] > 0])
        losses = len(group[group["pnl"] <= 0])
        pnl = group["pnl"].sum()
        ret_pct = (pnl / initial_capital) * 100
        stock_stats.append({
            "Stock": stock,
            "Trades": len(group),
            "Wins": wins,
            "Losses": losses,
            "P&L": pnl,
            "Return %": ret_pct
        })
        
    df_stock_stats = pd.DataFrame(stock_stats)
    df_stock_stats.to_csv(os.path.join(output_dir, "backtest_report.csv"), index=False)
    
    try:
        df_stock_stats.to_excel(os.path.join(output_dir, "backtest_report.xlsx"), index=False)
    except ModuleNotFoundError:
        print("openpyxl not installed, skipping xlsx generation.")

    print(f"Report generated successfully in {output_dir}")
