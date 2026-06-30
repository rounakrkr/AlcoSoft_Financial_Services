import os
import sys
import itertools
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from research.sweep_midcap50_engine import evaluate_config, load_cache, SystemConfig, LongEngineConfig, ShortEngineConfig

def main():
    print("Loading cache for 729 Custom Sweep...")
    cache_path = os.path.join(os.path.dirname(__file__), "midcap50_historical_cache.pkl")
    historical_data = load_cache(cache_path)
    print(f"Cache loaded. Starting full Cartesian sweep...")

    # Dimension 1: Long Base Params (from R1 top 3)
    d1_params = [
        {"sl": 0.007, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.008, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.010, "pt": 0.025, "rsi": 78.0}
    ]

    # Dimension 2: DYN_EXIT
    d2_params = ['DISABLE', 'EMA50', 'PROFIT_ONLY']

    # Dimension 3: Short Engine
    d3_params = [
        {"s_gap": -0.015, "s_rsi": 17.0},
        {"s_gap": -0.015, "s_rsi": 25.0},
        {"s_gap": -0.012, "s_rsi": 17.0}
    ]

    # Dimension 4: Long Entry Variant
    d4_params = ['VARIANT_E', 'VARIANT_D', 'BASELINE']

    # Dimension 5: Anti-STT (Hold time)
    d5_params = [20, 15, 0]

    # Dimension 6: Capital
    d6_params = [200000.0, 100000.0, 500000.0]

    all_combos = list(itertools.product(d1_params, d2_params, d3_params, d4_params, d5_params, d6_params))
    
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)
    
    leaderboard_file = os.path.join(results_dir, "custom_729_leaderboard.txt")
    csv_file = os.path.join(results_dir, "custom_729_all_runs.csv")
    
    with open(csv_file, 'w') as f:
        f.write("name,net_val,Total Trades,Win Rate,Gross Return,STT Impact\n")

    top_results = []
    best_net = -999.0
    best_tearsheet = ""

    count = 0
    total = len(all_combos)
    
    for c in all_combos:
        d1, dyn, d3, var, hold, cap = c
        count += 1
        name = f"C729_{count}"
        
        sys_cfg = SystemConfig(capital=cap, margin=5.0)
        long_cfg = LongEngineConfig(
            stop_loss_pct=d1["sl"],
            profit_target_pct=d1["pt"],
            rsi_exit_threshold=d1["rsi"],
            market_gap_threshold=0.007,
            market_breadth_requirement=0.35,
            partial_booking_fraction=0.25,
            dyn_exit_type=dyn,
            min_hold_time=hold,
            entry_variant=var
        )
        short_cfg = ShortEngineConfig(
            target_gap_threshold=d3["s_gap"],
            rsi_exit_threshold=d3["s_rsi"],
            stop_loss_pct=0.005,
            profit_target_pct=0.025
        )

        res, tearsheet, net_pct = evaluate_config(name, sys_cfg, long_cfg, short_cfg, historical_data, True)
        
        with open(csv_file, 'a') as f:
            f.write(f"{name},{net_pct:.2f},{res.get('Total Trades', 0)},{res.get('Win Rate', '0%')},{res.get('Gross Return', '0%')},{res.get('STT Impact', '0%')}\n")

        top_results.append((name, net_pct, res, tearsheet, sys_cfg, long_cfg, short_cfg))
        top_results.sort(key=lambda x: x[1], reverse=True)
        top_results = top_results[:10]

        if net_pct > best_net:
            best_net = net_pct
            best_tearsheet = tearsheet
            with open(os.path.join(results_dir, "BEST_729_CONFIG_TEARSHEET.txt"), 'w') as f:
                f.write(best_tearsheet)

        if count % 50 == 0 or count == total:
            # Update leaderboard
            with open(leaderboard_file, 'w') as f:
                f.write("CUSTOM 729 CARTESIAN SWEEP LEADERBOARD\n")
                f.write("=================================================================\n")
                for i, tr in enumerate(top_results):
                    name_str, npct, rdict, _, _, lcfg, scfg = tr
                    f.write(f" #{i+1} | {name_str} | Net={npct:.2f}% | WR={rdict.get('Win Rate','')} | T={rdict.get('Total Trades','')}\n")
            print(f"Processed {count}/{total}... Best Net: {top_results[0][1]:.2f}%")

    print(f"Sweep complete! Best Net Return: {best_net:.2f}%")

if __name__ == '__main__':
    main()
