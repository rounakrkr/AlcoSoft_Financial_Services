import re

with open("run_midcap50_historical_backtest.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Modify LongEngineConfig to include disable_dyn_exit
content = content.replace("stop_loss_pct: float = 0.008              # Flat 0.8% SL",
                          "stop_loss_pct: float = 0.008\n    disable_dyn_exit: bool = False\n    max_reentries: int = 99")

# 2. Modify ShortEngineConfig
content = content.replace("stop_loss_pct: float = 0.005              # Flat 0.5% SL",
                          "stop_loss_pct: float = 0.005\n    disable_shorts: bool = False\n    savior_exit: bool = False")

# 3. IndicatorPreprocessor: add ema9
content = content.replace('df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).fillna(method="bfill")',
                          'df["ema21"] = ta.trend.ema_indicator(df["close"], window=21).fillna(method="bfill")\n            df["ema9"] = ta.trend.ema_indicator(df["close"], window=9).fillna(method="bfill")')

# 4. LongEngineExecutor._manage_positions: wrap DYN_EXIT
content = content.replace("# E. Dynamic Exit (SELL_EMA_MOMENTUM_LOSS)",
                          "if not self.config.disable_dyn_exit:\n                # E. Dynamic Exit")

dyn_exit_block_start = content.find("if not self.config.disable_dyn_exit:\n                # E. Dynamic Exit")
dyn_exit_block_end = content.find("# Clean up closed positions", dyn_exit_block_start)
block = content[dyn_exit_block_start:dyn_exit_block_end]
new_block = block.replace("\\n            ", "\\n                ")
new_block = new_block.replace("if not self.config.disable_dyn_exit:\\n                    # E. Dynamic Exit", "if not self.config.disable_dyn_exit:\\n                # E. Dynamic Exit")
content = content[:dyn_exit_block_start] + new_block + content[dyn_exit_block_end:]

# 5. ShortEngineExecutor execute: handle disable_shorts
content = content.replace("def execute(self) -> List[TradeRecord]:\\n        logger.info(\\"Executing Short Engine Backtest...\\")",
                          "def execute(self) -> List[TradeRecord]:\\n        if self.config.disable_shorts:\\n            return []\\n        logger.info(\\"Executing Short Engine Backtest...\\")")

# 6. ShortEngineExecutor._manage_positions: add savior exit
short_stop_loss = \"\"\"# B. Stop Loss
            if pos.stop_loss_price is not None and hp >= pos.stop_loss_price:
                exit_price = max(pos.stop_loss_price, op)
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                    direction="SHORT", exit_reason="STOP_LOSS"
                ))
                syms_to_close.append(sym)
                continue\"\"\"
                
savior_exit = \"\"\"# B. Stop Loss
            if pos.stop_loss_price is not None and hp >= pos.stop_loss_price:
                exit_price = max(pos.stop_loss_price, op)
                self.trades.append(TradeRecord(
                    symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                    entry_price=pos.entry_price, exit_price=exit_price, quantity=pos.quantity,
                    direction="SHORT", exit_reason="STOP_LOSS"
                ))
                syms_to_close.append(sym)
                continue
                
            # B2. Savior Exit
            if self.config.savior_exit and ts > pos.entry_time:
                curr_ema9 = df["ema9"].iloc[idx]
                if cp > curr_ema9:
                    self.trades.append(TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=cp, quantity=pos.quantity,
                        direction="SHORT", exit_reason="SAVIOR_EXIT"
                    ))
                    syms_to_close.append(sym)
                    continue\"\"\"
content = content.replace(short_stop_loss, savior_exit)

# 7. Remove main() and replace with sweeping logic
content = content[:content.find("def main():")]

sweep_logic = \"\"\"
import itertools

def evaluate_config(sys_config, long_config, short_config, stock_dfs, regime_analyzer, signal_gen):
    long_executor = LongEngineExecutor(
        sys_config=sys_config,
        long_config=long_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.long_signals
    )
    long_trades = long_executor.execute()
    
    short_executor = ShortEngineExecutor(
        sys_config=sys_config,
        short_config=short_config,
        stock_dfs=stock_dfs,
        regime_analyzer=regime_analyzer,
        signals=signal_gen.short_signals
    )
    short_trades = short_executor.execute()
    
    trades = long_trades + short_trades
    gross_pnl = sum(t.pnl_gross for t in trades)
    stt_tax = sum(t.stt_tax for t in trades)
    net_pnl = gross_pnl - stt_tax
    wins = len([t for t in trades if t.pnl_net > 0])
    wr = wins / len(trades) * 100 if trades else 0.0
    expectancy = net_pnl / len(trades) if trades else 0.0
    return {
        'trades': len(trades),
        'wr': wr,
        'gross': gross_pnl / sys_config.capital * 100,
        'stt': -stt_tax / sys_config.capital * 100,
        'net': net_pnl / sys_config.capital * 100,
        'expectancy': expectancy,
        'long_trades': long_trades,
        'short_trades': short_trades
    }

def update_leaderboard(entries, filename="results/midcap50_top10_leaderboard.txt"):
    import os
    if not os.path.exists("results"):
        os.makedirs("results")
    
    entries.sort(key=lambda x: x['stats']['net'], reverse=True)
    top10 = entries[:10]
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write("MIDCAP 50 DUAL ENGINE TOP 10 LEADERBOARD\\n")
        f.write("="*140 + "\\n")
        for i, e in enumerate(top10):
            s = e['stats']
            f.write(f"  # {i+1:<2} | {e['name']:<18} | {e['params']:<40} | WR={s['wr']:<5.2f}% | T={s['trades']:<4} | Gross={s['gross']:<6.2f}% | STT={s['stt']:<6.2f}% | NET={s['net']:<6.2f}% | Exp=₹{s['expectancy']:<6.2f} per trade | Diff={e['diff']}\\n")
        f.write("="*140 + "\\n")

def run_sweeps():
    logger.setLevel(logging.WARNING) # reduce spam
    stock_dfs = load_cache()
    IndicatorPreprocessor.enrich_data(stock_dfs)
    regime_analyzer = MarketRegimeAnalyzer(stock_dfs)
    signal_gen = SignalGenerator(stock_dfs)
    signal_gen.precompute_signals()
    
    all_results = []
    sys_base = SystemConfig()
    
    print("Phase A: Evaluating Short Engine Fixes (Disable vs Savior Exit vs Tight Selection)")
    res = evaluate_config(sys_base, LongEngineConfig(disable_dyn_exit=True), ShortEngineConfig(), stock_dfs, regime_analyzer, signal_gen)
    all_results.append({'name': 'BASE_DYN_OFF', 'params': 'baseline short, dyn_off long', 'diff': 'baseline', 'stats': res})
    
    res = evaluate_config(sys_base, LongEngineConfig(disable_dyn_exit=True), ShortEngineConfig(disable_shorts=True), stock_dfs, regime_analyzer, signal_gen)
    all_results.append({'name': 'NO_SHORTS', 'params': 'disable_shorts=True', 'diff': 'shorts off', 'stats': res})
    
    res = evaluate_config(sys_base, LongEngineConfig(disable_dyn_exit=True), ShortEngineConfig(savior_exit=True), stock_dfs, regime_analyzer, signal_gen)
    all_results.append({'name': 'SAVIOR_EXIT', 'params': 'savior_exit=True', 'diff': 'savior exit on', 'stats': res})
    
    res = evaluate_config(sys_base, LongEngineConfig(disable_dyn_exit=True), ShortEngineConfig(target_gap_threshold=-0.012), stock_dfs, regime_analyzer, signal_gen)
    all_results.append({'name': 'TIGHT_GAP', 'params': 'gap=-0.012', 'diff': 'gap selection tight', 'stats': res})
    
    update_leaderboard(all_results)
    
    # We found that NO_SHORTS works best based on -19% net loss in short engine
    print("Phase B: Sweeping Long Engine Parameters (with shorts disabled)")
    sls = [0.005, 0.008, 0.010, 0.012, 0.015, 0.018, 0.020, 0.025]
    pts = [0.010, 0.015, 0.020, 0.025, 0.030, 0.040, 0.050]
    rsis = [68, 72, 76, 80, 85, 88]
    
    total = len(sls) * len(pts) * len(rsis)
    print(f"Total combinations for Phase B: {total}")
    
    count = 0
    for sl in sls:
        for pt in pts:
            for rsi in rsis:
                count += 1
                if count % 20 == 0:
                    print(f"Progress: {count}/{total}")
                    update_leaderboard(all_results)
                
                res = evaluate_config(
                    sys_base,
                    LongEngineConfig(disable_dyn_exit=True, stop_loss_pct=sl, profit_target_pct=pt, rsi_exit_threshold=rsi),
                    ShortEngineConfig(disable_shorts=True),
                    stock_dfs, regime_analyzer, signal_gen
                )
                all_results.append({
                    'name': f'L_SWEEP_{count}',
                    'params': f'SL={sl}, PT={pt}, RSI={rsi}',
                    'diff': 'sweep',
                    'stats': res
                })
                
    update_leaderboard(all_results)
    print("Sweeps completed. Generating tearsheet for top config.")
    all_results.sort(key=lambda x: x['stats']['net'], reverse=True)
    best = all_results[0]
    out_path = r"C:\\Users\\RounakKR\\.gemini\\antigravity\\brain\\0fe8b862-bd60-467b-9314-d95559e8cba9\\BEST_CONFIG_TEARSHEET.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Best Config Net: {best['stats']['net']}%")
    ReportingEngine.print_tearsheet(best['stats']['long_trades'], best['stats']['short_trades'], sys_base.capital)

if __name__ == "__main__":
    run_sweeps()
\"\"\"

content += sweep_logic

with open("fast_sweep.py", "w", encoding="utf-8") as f:
    f.write(content)
print("fast_sweep.py created successfully.")
