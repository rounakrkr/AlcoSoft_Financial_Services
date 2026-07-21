# ROBUSTNESS ANALYSIS — Midcap50 Optimized Config
_Generated: 2026-07-21T18:45:56_

**Setup:** 45 Midcap50 symbols, 5-min candles, Jan 2022 → Jul 2026.
**Config:** long_sl 1.2% · long_pt 3.5% · long_pp 0.75 · RSI-exit OFF · TSL 1.2x/0.5%; short_sl 0.5% · short_pt 2.0% · short_target_gap −2.5%; bull 0.7%/35% · bear −0.6%/40%; maxpos 1; 5× margin; ₹1L; daily compound.
**Base result:** ₹1L → ₹802,772 on 363 trades.

## 1. Complete metrics table

| Metric | Full | Long only | Short only |
|---|---:|---:|---:|
| Trades | 363 | 328 | 35 |
| Net PnL ₹ | 702772 | 541710 | 161062 |
| Return % | 702.8 | 541.7 | 161.1 |
| Win rate % | 45.5 | 43.6 | 62.9 |
| Avg winner ₹ | 13522.0 | 13630.0 | 12819.0 |
| Avg loser ₹ | -7719.0 | -7607.0 | -9304.0 |
| Profit Factor | 1.46 | 1.38 | 2.33 |
| Max DD % | -30.2 | -39.9 | -10.6 |
| Sharpe (annualized) | 3.38 | 3.08 | 7.94 |
| Max losing streak | 8 | 6 | 3 |
| Total months | 47 | 44 | 18 |
| Negative months | 14 | 14 | 5 |
| % negative months | 29.8 | 31.8 | 27.8 |

### Per-year breakdown (full portfolio, includes compounding)

| Year | Trades | Win rate % | Return % | PF | Max DD % |
|---|---:|---:|---:|---:|---:|
| 2022.0 | 146.0 | 41.8 | 44.1 | 1.12 | -29.5 |
| 2023.0 | 39.0 | 43.6 | 38.7 | 1.62 | -10.9 |
| 2024.0 | 85.0 | 47.1 | 73.1 | 1.43 | -30.2 |
| 2025.0 | 47.0 | 55.3 | 57.6 | 1.69 | -11.7 |
| 2026.0 | 46.0 | 45.7 | 47.2 | 1.58 | -11.2 |

## 2. Walk-forward validation (out-of-sample stability)

> **Note:** True walk-forward re-optimization (train per fold → apply to test) is a P1 backlog item explicitly deferred by the user. This section applies the SAME optimized config to each sub-period so you can see whether the parameter set is independently profitable in each fold.

| Period | Trades | Ret % | PF | Win % | Max DD % | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN 2022-24 | 270 | 246.1 | 1.31 | 43.7 | -30.2 | 2.76 |
| VAL 2025-26 | 93 | 456.7 | 1.62 | 50.5 | -21.4 | 6.16 |
| 2025 only | 47 | 199.5 | 1.69 | 55.3 | -21.4 | 6.76 |
| 2026 only | 46 | 257.2 | 1.58 | 45.7 | -24.0 | 6.87 |

**Read:** If both 2025 and 2026 are independently profitable with PF > 1.2, the edge is not a one-year artefact. If one year carries most of the val-period result, treat val PF cautiously.

## 3. Top-5 trade dependency & equity curve smoothness

### Top-5 winning trades (₹ PnL)

| Rank | Symbol | Dir | Entry | Exit | Qty | Net ₹ | Reason | Entry time | Exit time |
|---:|---|---|---:|---:|---:|---:|---|---|---|
| 1 | ASHOKLEY | LONG | 146.11 | 151.22 | 20146 | 101,956 | PARTIAL_PROFIT | 2026-06-12 12:55:00+05:30 | 2026-06-12 14:40:00+05:30 |
| 2 | BALKRISIND | LONG | 2263.90 | 2343.14 | 1206 | 94,569 | PARTIAL_PROFIT | 2026-04-15 14:10:00+05:30 | 2026-04-15 15:15:00+05:30 |
| 3 | BANKINDIA | SHORT | 139.66 | 136.87 | 23725 | 65,119 | PARTIAL_PROFIT | 2026-03-30 12:35:00+05:30 | 2026-03-30 15:00:00+05:30 |
| 4 | APOLLOTYRE | LONG | 456.65 | 467.00 | 5916 | 60,260 | EOD | 2025-08-18 12:20:00+05:30 | 2025-08-18 15:15:00+05:30 |
| 5 | GMRAIRPORT | LONG | 89.03 | 90.33 | 39878 | 50,419 | TSL | 2026-04-01 10:45:00+05:30 | 2026-04-01 13:40:00+05:30 |

**Top-5 = 53.0% of total net PnL** (₹372,326 of ₹702,771)

### Metrics with top-5 removed

| Metric | Original | Top-5 removed |
|---|---:|---:|
| Trades | 363 | 358 |
| Net PnL ₹ | 702772 | 330445 |
| Return % | 702.8 | 330.4 |
| Win rate % | 45.5 | 44.7 |
| Profit Factor | 1.46 | 1.22 |
| Max DD % | -30.2 | -30.7 |
| Sharpe | 3.38 | 2.53 |

### Month-end equity curve (compounded, ₹)

| Month | Equity ₹ | Month return % |
|---|---:|---:|
| 2022-01 | 101,607 | 1.6 |
| 2022-03 | 118,712 | 16.8 |
| 2022-04 | 156,751 | 32.0 |
| 2022-05 | 155,283 | -0.9 |
| 2022-06 | 165,727 | 6.7 |
| 2022-07 | 188,120 | 13.5 |
| 2022-08 | 173,707 | -7.7 |
| 2022-09 | 147,130 | -15.3 |
| 2022-10 | 139,457 | -5.2 |
| 2022-11 | 147,553 | 5.8 |
| 2022-12 | 144,125 | -2.3 |
| 2023-01 | 146,379 | 1.6 |
| 2023-02 | 150,193 | 2.6 |
| 2023-03 | 153,727 | 2.4 |
| 2023-04 | 156,562 | 1.8 |
| 2023-05 | 187,388 | 19.7 |
| 2023-08 | 171,201 | -8.6 |
| 2023-09 | 188,912 | 10.3 |
| 2023-10 | 180,997 | -4.2 |
| 2023-11 | 203,818 | 12.6 |
| 2023-12 | 199,916 | -1.9 |
| 2024-01 | 235,792 | 17.9 |
| 2024-02 | 179,076 | -24.1 |
| 2024-03 | 174,790 | -2.4 |
| 2024-04 | 221,950 | 27.0 |
| 2024-05 | 276,970 | 24.8 |
| 2024-06 | 325,930 | 17.7 |
| 2024-07 | 356,275 | 9.3 |
| 2024-08 | 320,045 | -10.2 |
| 2024-09 | 327,912 | 2.5 |
| 2024-10 | 344,119 | 4.9 |
| 2024-11 | 346,058 | 0.6 |
| 2025-01 | 441,433 | 27.6 |
| 2025-02 | 457,619 | 3.7 |
| 2025-03 | 502,434 | 9.8 |
| 2025-04 | 580,449 | 15.5 |
| 2025-05 | 596,780 | 2.8 |
| 2025-06 | 540,331 | -9.5 |
| 2025-08 | 595,355 | 10.2 |
| 2025-09 | 545,541 | -8.4 |
| 2026-01 | 572,173 | 4.9 |
| 2026-02 | 619,239 | 8.2 |
| 2026-03 | 710,080 | 14.7 |
| 2026-04 | 777,679 | 9.5 |
| 2026-05 | 791,703 | 1.8 |
| 2026-06 | 885,602 | 11.9 |
| 2026-07 | 802,772 | -9.4 |

## 4. No-leverage, no-compounding (raw edge)

> Fixed **₹1,00,000 per trade**, **1× margin (no leverage)**, **no daily compounding**. This strips out amplification and shows the raw per-trade edge.

| Metric | Value |
|---|---:|
| Trades | 363 |
| Total net PnL ₹ | 50,035 |
| Total return % (on single ₹1L allocation, sum of all trades) | 50.0 |
| Avg PnL per trade ₹ | 138 |
| Avg winner ₹ | 943 |
| Avg loser ₹ | -533 |
| Win rate % | 45.5 |
| Profit Factor | 1.47 |

### Per-year (no leverage, no compounding — capital reset ₹1L each trade)

| Year | Trades | Win rate % | Net ₹ | PF |
|---|---:|---:|---:|---:|
| 2022 | 146 | 41.8 | 10,860 | 1.22 |
| 2023 | 39 | 43.6 | 7,419 | 1.76 |
| 2024 | 85 | 47.1 | 13,230 | 1.54 |
| 2025 | 47 | 55.3 | 9,864 | 1.9 |
| 2026 | 46 | 45.7 | 8,659 | 1.75 |

## 5. EMA50_DYN exit — necessary evil or leak?

### EMA50_DYN isolated stats (optimized run)

| Metric | Value |
|---|---:|
| Trades exited via EMA50_DYN | 145 |
| Total PnL from EMA50 exits ₹ | -952,091 |
| Win rate % | 5.5 |
| Avg PnL per EMA50 exit ₹ | -6,566 |
| Avg winner in EMA50 subset ₹ | 2,286 |
| Avg loser in EMA50 subset ₹ | -7,083 |

### Replaced EMA50_DYN with 20-candle time exit (`TIME_EXIT`)

| Metric | Original (EMA50) | Time-exit 20 candles |
|---|---:|---:|
| Trades | 363 | 470 |
| Return % | 702.8 | 425.5 |
| Profit Factor | 1.46 | 1.39 |
| Win rate % | 45.5 | 51.1 |
| Max DD % | -30.2 | -51.4 |
| Sharpe | 3.38 | 2.72 |
| Max losing streak | 8 | 8 |

### Time-exit variant — exit reason breakdown

| Reason | Count | Net ₹ |
|---|---:|---:|
| EOD | 124 | 197,028 |
| PARTIAL_PROFIT | 8 | 168,122 |
| SL | 64 | -571,196 |
| TIME_EXIT | 242 | 339,195 |
| TSL | 32 | 292,333 |

**Read:** If time-exit variant delivers ≥ EMA50 return with equal or better DD & Sharpe, EMA50_DYN is a leak. If EMA50 wins → it's a *necessary evil* (rides trend when active, cuts decayed signals into small losses that are still net-favourable overall).

## 6. Short engine robustness — per-year breakdown

### Short-only per year (5x margin, compounded)

| Year | Trades | Win rate % | Net ₹ | Ret % | PF | Max DD % |
|---|---:|---:|---:|---:|---:|---:|
| 2022.0 | 12.0 | 66.7 | 24,753 | 24.8 | 2.86 | -9.3 |
| 2023.0 | 2.0 | 100.0 | 16,357 | 13.1 | 99.0 | 0.0 |
| 2024.0 | 7.0 | 57.1 | 21,505 | 15.2 | 1.95 | -6.4 |
| 2025.0 | 5.0 | 60.0 | 14,244 | 8.8 | 1.53 | -10.6 |
| 2026.0 | 9.0 | 55.6 | 84,202 | 47.6 | 2.45 | -2.5 |

### Long-only per year (for reference)

| Year | Trades | Win rate % | Net ₹ | Ret % | PF | Max DD % |
|---|---:|---:|---:|---:|---:|---:|
| 2022.0 | 134.0 | 39.6 | 19,373 | 19.4 | 1.05 | -39.9 |
| 2023.0 | 37.0 | 40.5 | 39,434 | 33.0 | 1.44 | -15.8 |
| 2024.0 | 78.0 | 46.2 | 124,637 | 78.5 | 1.4 | -34.1 |
| 2025.0 | 42.0 | 54.8 | 185,239 | 65.4 | 1.71 | -11.1 |
| 2026.0 | 37.0 | 43.2 | 173,028 | 36.9 | 1.45 | -14.0 |

**Read:** If shorts are net-negative in ≥2 of 5 years, the short engine is unproven and profits hinge on regime rarity. Consider running long-only during weak years.

---

### Files
- `research/robustness_optimized_trades.csv` — full baseline trades
- `research/robustness_no_compound_trades.csv` — no-leverage variant trades
- `research/robustness_time_exit_trades.csv` — 20-candle time-exit variant trades