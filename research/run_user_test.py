import sys
import os
import json
import pandas as pd

# Path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Temporarily force the trading_settings.json for this exact test
settings_path = os.path.join(ROOT, "config", "trading_settings.json")
with open(settings_path, 'r') as f:
    config = json.load(f)

# Modify for User's exact request: No upper limit, 1.2 TSL activation, 0.2 trail
config['risk']['stop_loss_percent'] = 0.01
config['risk']['trailing_sl_percent'] = 0.002
config['risk']['tsl_activation_ratio'] = 1.2
config['risk']['target_rr_ratio'] = 100.0  # No upper limit
config['risk']['allow_margin'] = True
config['risk']['margin_leverage'] = 5.0

with open(settings_path, 'w') as f:
    json.dump(config, f, indent=2)

from research.backtest_runner import BacktestRunner

print("Running Backtest Sweep against SELL_EMA_MOMENTUM_LOSS")
print("Capital: 5000, Margin: Enabled (5x), Target: Unlimited, TSL: 1.2% / 0.2%")

strategies_to_test = [
    "BUY_STREAK_MOMENTUM_BREAKOUT",
    "BUY_STREAK_MOMENTUM_BREAKOUT_VOL",
    "BUY_STREAK_MOMENTUM_BREAKOUT_MACD",
    "BUY_STREAK_MOMENTUM_BREAKOUT_SOLID"
]

results = []

for strat in strategies_to_test:
    print(f"\n--- Testing {strat} ---")
    try:
        runner = BacktestRunner(
            initial_capital=5000.0,
            buy_strategy_name=strat,
            sell_strategy_name="SELL_EMA_MOMENTUM_LOSS",
            use_margin=True
        )
        trades = runner.run()
        
        if not trades:
            print("No trades executed.")
            continue
            
        df = pd.DataFrame(trades)
        total_pnl = df['pnl'].sum()
        total_return_pct = (total_pnl / 5000.0) * 100
        winning_trades = len(df[df['pnl'] > 0])
        win_rate = (winning_trades / len(df)) * 100
        
        print(f"Total Trades: {len(df)}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Total Return: {total_return_pct:.2f}%")
        
        results.append({
            "strategy": strat,
            "win_rate": win_rate,
            "return": total_return_pct,
            "trades": len(df)
        })
    except Exception as e:
        print(f"Failed to test {strat}: {e}")

print("\n=== FINAL RANKING ===")
results.sort(key=lambda x: x['win_rate'], reverse=True)
for r in results:
    print(f"{r['strategy']}: {r['win_rate']:.2f}% WR | {r['return']:.2f}% Ret | {r['trades']} Trades")
