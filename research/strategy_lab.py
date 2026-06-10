import sys
import os
import json

# Add root directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.backtest_runner import BacktestRunner
from research.report_generator import generate_report

def load_strategies():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "strategy_sets.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("buy_sets", []), data.get("sell_sets", [])

def main():
    print("="*50)
    print("ALCOSOFT STRATEGY LAB (BACKTESTER)")
    print("="*50)
    
    try:
        capital_input = input("\nEnter Capital: ")
        initial_capital = float(capital_input)
    except ValueError:
        print("Invalid capital amount. Exiting.")
        return

    buy_sets, sell_sets = load_strategies()
    
    print("\n--- BUY STRATEGIES ---")
    for i, s in enumerate(buy_sets, 1):
        print(f"{i}. {s['name']}")
        
    buy_choice = input("\nSelect ONE BUY strategy (enter number): ")
    try:
        buy_idx = int(buy_choice) - 1
        selected_buy = buy_sets[buy_idx]["name"]
    except (ValueError, IndexError):
        print("Invalid selection. Exiting.")
        return

    print("\n--- SELL STRATEGIES ---")
    for i, s in enumerate(sell_sets, 1):
        print(f"{i}. {s['name']}")
        
    sell_choice = input("\nSelect ONE SELL strategy (enter number): ")
    try:
        sell_idx = int(sell_choice) - 1
        selected_sell = sell_sets[sell_idx]["name"]
    except (ValueError, IndexError):
        print("Invalid selection. Exiting.")
        return

    use_margin_input = input("\nUse Margin (5x leverage) for positioning? (y/n): ")
    use_margin = use_margin_input.lower().startswith('y')

    print("\n" + "="*50)
    print(f"CONFIGURATION:")
    print(f"Capital: ₹{initial_capital:,.2f}")
    print(f"Margin Allowed: {'Yes' if use_margin else 'No'}")
    print(f"BUY:  {selected_buy}")
    print(f"SELL: {selected_sell}")
    print("="*50 + "\n")
    
    runner = BacktestRunner(initial_capital, selected_buy, selected_sell, use_margin)
    trades = runner.run()
    
    print("\nGenerating Reports...")
    generate_report(trades, initial_capital)
    print("Done! Check research/results/ for the output files.")

if __name__ == "__main__":
    main()
