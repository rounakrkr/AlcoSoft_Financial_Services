import os

with open('sweep_midcap50_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

parts = content.split("def main():")
header = parts[0]

new_main = """def main():
    import itertools
    print("Loading 44-stock DataFrame cache for Midcap 50...")
    stock_dfs = load_cache()
    if not stock_dfs: return
    
    print("Enriching data...")
    IndicatorPreprocessor.enrich_data(stock_dfs)
    
    print("Analyzing market regime...")
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)

    d1_params = [
        {"sl": 0.007, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.008, "pt": 0.025, "rsi": 78.0},
        {"sl": 0.010, "pt": 0.025, "rsi": 78.0}
    ]
    d2_params = ['DISABLE', 'EMA50', 'PROFIT_ONLY']
    d3_params = [
        {"s_gap": -0.015, "s_rsi": 17.0},
        {"s_gap": -0.015, "s_rsi": 25.0},
        {"s_gap": -0.012, "s_rsi": 17.0}
    ]
    d4_params = ['VARIANT_E', 'VARIANT_D', 'BASELINE']
    d5_params = [20, 15, 0]
    d6_params = [200000.0, 100000.0, 500000.0]

    all_combos = list(itertools.product(d1_params, d2_params, d3_params, d4_params, d5_params, d6_params))
    total = len(all_combos)
    
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)
    
    leaderboard_file = os.path.join(results_dir, "custom_729_leaderboard.txt")
    csv_file = os.path.join(results_dir, "custom_729_all_runs.csv")
    tearsheet_file = os.path.join(results_dir, "BEST_729_CONFIG_TEARSHEET.txt")
    
    with open(csv_file, 'w') as f:
        f.write("name,net_val,Total Trades,Win Rate,Gross Return,STT Impact\\n")

    top_results = []
    best_net = -999.0

    print(f"Executing {total} combinations...")
    count = 0
    
    cached_signals = {}
    sig_generator = SignalGenerator(stock_dfs)
    for v in d4_params:
        print(f"Precomputing signals for {v}...")
        try:
            l_s, l_tws, l_sbts = sig_generator.precompute_signals(entry_variant=v)
        except TypeError: # If it doesn't take entry_variant
            sig_generator.precompute_signals()
            l_s = getattr(sig_generator, 'long_signals', {})
            l_tws = getattr(sig_generator, 'long_ts_with_signals', set())
            l_sbts = getattr(sig_generator, 'long_signals_by_ts', {})
        
        try:
            s_s, s_tws, s_sbts = sig_generator.precompute_signals(entry_variant="SHORT_BASELINE")
        except TypeError:
            s_s = getattr(sig_generator, 'short_signals', {})
            s_tws = getattr(sig_generator, 'short_ts_with_signals', set())
            s_sbts = getattr(sig_generator, 'short_signals_by_ts', {})
            
        cached_signals[v] = (l_s, l_tws, l_sbts, s_s, s_tws, s_sbts)

    for c in all_combos:
        d1, dyn, d3, var, hold, cap = c
        count += 1
        name = f"C729_{count}"
        
        sys_config = SystemConfig(capital=cap, margin=5.0)
        long_config = LongEngineConfig(
            stop_loss_pct=d1["sl"],
            profit_target_pct=d1["pt"],
            rsi_exit_threshold=d1["rsi"],
            market_gap_threshold=0.007,
            market_breadth_requirement=0.35,
            partial_booking_fraction=0.25,
            dyn_exit_type=dyn,
            min_hold_time=hold,
            entry_variant=var,
            reentry_cap=9999
        )
        short_config = ShortEngineConfig(
            target_gap_threshold=d3["s_gap"],
            rsi_exit_threshold=d3["s_rsi"],
            stop_loss_pct=0.005,
            profit_target_pct=0.025,
            market_gap_threshold=-0.006,
            market_breadth_requirement=0.4,
            disable_shorts=False,
            savior_exit=False
        )

        l_s, l_tws, l_sbts, s_s, s_tws, s_sbts = cached_signals[var]
        
        long_trades, short_trades, combined_metrics = run_backtest(
            sys_config, long_config, short_config, stock_dfs, regime_analyzer,
            l_s, s_s, l_tws, l_sbts, s_tws, s_sbts, verbose=False
        )

        net_pct = combined_metrics.get("Absolute Net Return", 0)
        with open(csv_file, 'a') as f:
            f.write(f"{name},{net_pct:.2f},{combined_metrics.get('Total Trades', 0)},{combined_metrics.get('Win Rate', '0%')},{combined_metrics.get('Gross Return', '0%')},{combined_metrics.get('STT Impact', '0%')}\\n")

        top_results.append((name, net_pct, combined_metrics, long_trades, short_trades, sys_config))
        top_results.sort(key=lambda x: x[1], reverse=True)
        top_results = top_results[:10]

        if net_pct > best_net:
            best_net = net_pct
            tearsheet = ReportingEngine.print_tearsheet(long_trades, short_trades, sys_config.buying_power())
            with open(tearsheet_file, 'w') as f:
                f.write(tearsheet)

        if count % 20 == 0 or count == total:
            with open(leaderboard_file, 'w') as f:
                f.write("CUSTOM 729 CARTESIAN SWEEP LEADERBOARD\\n")
                f.write("=================================================================\\n")
                for i, tr in enumerate(top_results):
                    n_str, n_pct, r_dict, _, _, _ = tr
                    f.write(f" #{i+1} | {n_str} | Net={n_pct:.2f}% | WR={r_dict.get('Win Rate','')} | T={r_dict.get('Total Trades','')}\\n")
            print(f"Processed {count}/{total}... Best Net: {top_results[0][1]:.2f}%")

if __name__ == '__main__':
    main()
"""

with open('sweep_729.py', 'w', encoding='utf-8') as f:
    f.write(header + "\\n" + new_main)

print("Generated sweep_729.py successfully.")
