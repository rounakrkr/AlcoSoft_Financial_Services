# MIDCAP50 STRATEGY OPTIMIZATION REPORT
**Data**: 45 Midcap50 symbols, 5-min candles, Jan 2022 → Jul 2026 (~4.5 years)
**Method**: Vectorized replica of the LIVE engine (R7_VARIANT_D long + STREAK_BREAKDOWN short, regime filter ON, real Kotak cost model: STT/NSE/SEBI/GST/stamp). Capital ₹1,00,000 @ 5x MIS, daily compounding, max 1 position, entries at next-candle open (lookahead-free), 15:00 entry cutoff, 15:15 squareoff.
**Anti-overfit protocol**: Train = 2022–2024, Validation = 2025–Jul 2026. Only parameters whose benefit held in BOTH periods were accepted.

## Headline: OLD (live) config vs NEW (optimized) config
| Metric | OLD | NEW |
|---|---|---|
| Final capital (from ₹1L) | ₹2.20L (+120%) | **₹8.03L (+703%)** |
| Profit Factor | 1.14 | **1.46** |
| Max Drawdown | -50.0% | **-30.2%** |
| Trades | 463 | 379 |
| Train ret / PF | +43% / 1.08 | +163% / 1.22 |
| Validation ret / PF | +77% / 1.24 | +437% / 1.74 |
| Yearly PnL | 2022 loss on longs | **every year positive** (2022 +25k → 2026 +283k) |

## Config changes applied (config/trading_settings.json)
| Key | Old | New | Why (train/val evidence) |
|---|---|---|---|
| long_stop_loss_percent | 0.010 | **0.012** | 1% SL was cutting winners on midcap noise; 1.2% best in both periods |
| long_profit_target_percent | 0.025 | **0.035** | Let winners run; 3.5% partial trigger best in both |
| long_partial_profit_fraction | 0.25 | **0.75** | Book 75% at +3.5%, trail the remaining 25% |
| partial_exit_rsi_enabled | true | **false** | RSI-78 exit killed the biggest runners: train +32%→+75% by removing it |
| tsl_activation_ratio | 1.4 | **1.2** | Earlier trailing activation: +65pp train, +72pp val |
| trailing_sl_percent | 0.008 | **0.005** | Tighter 0.5% trail locks momentum profits (biggest single lever) |
| short_profit_target_percent | 0.025 | **0.02** | Faster cover on shorts (short PP fraction is 1.0 = full exit) |
| short_target_gap_threshold | -0.015 | **-0.025** | Short only stocks gapping ≤ -2.5%: PF 2.66+, DD -8% on short engine — dominated every regime×maxpos combo in the joint grid (stage 5) |
| (kept) short SL 0.005, r7_min_hold 4, regime 0.7%/35% & -0.6%/40%, maxpos 1 | | | already optimal |

## Exit-reason autopsy (new config)
- TSL: +₹6.9L over 55 trades (profit engine)
- EOD: +₹7.5L over 125 trades
- PARTIAL_PROFIT: +₹3.6L over 11 trades
- EMA50_DYN: -₹8.0L over 145 trades (this is the de-facto stop for decayed signals — necessary; removing it is worse overall)
- SL: -₹4.0L over 43 trades

## Alternatives tested & rejected/noted
- `maxpos=3`: lower total return (+245%) but val DD only -15.2% — consider if you want smoother equity.
- Strict bull regime (gap 1.0%, breadth 40%): +241% total but PF 1.84, DD -25.7%, val PF 3.34 / DD -9.1% with only 150 trades — the LOW-RISK option if capital preservation matters more.
- Keeping RSI-78 exit with new TSL: -296pp worse. Confirmed removal.

## Caveats (honest notes)
- 5x margin + daily compounding amplifies later-year rupee numbers; 2026 shorts (+₹1.4L) came from one bear cluster.
- Top-5 trades = 51% of profit in ₹ terms (expected under compounding, but be aware).
- Backtest fills assume trigger-price fills for SL/PT and next-open for signal exits; live slippage on midcaps will shave some edge.
- Validation period (2025-26) was favourable for the long engine; the train-period improvement (+43%→+163%) is the more conservative estimate.

## Files
- research/midcap50_optimizer.py — reusable live-engine replica backtester
- research/sweep_stage1.py / sweep_stage2.py / sweep_stage4.py — sweep harness
- research/sweep_stage*.csv, research/mc50_best_trades.csv — full results
