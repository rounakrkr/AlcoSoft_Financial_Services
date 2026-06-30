# 🕵️ Enterprise Long Buy Postmortem & Universe Sweep

> [!NOTE]
> This exhaustive sweep evaluated over 70 multi-dimensional permutations, computing Gross PnL, exactly calculated STT taxes (0.035%), Win Rates, Max Drawdowns, and Profit Factors across 3 distinct universe filtering modes.

## 🏆 Top 10 Golden Configurations (Ranked by NET Return)

| Base Gap | Mkt Breadth | Universe Mode | Trades | Win Rate | Gross % | Net % | Max DD % | Profit Factor |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `>= 1.0%` | `>= 40%` | `ALL_STOCKS` | 233 | 64.8% | `+23.39%` | **+11.21%** | 3.9% | 1.76 |
| `>= 0.4%` | `>= 50%` | `ALL_STOCKS` | 465 | 62.2% | `+33.74%` | **+9.13%** | 5.7% | 1.52 |
| `>= 1.0%` | `>= 40%` | `EXCLUDE_GAP_DOWN` | 236 | 63.1% | `+21.26%` | **+8.85%** | 4.0% | 1.68 |
| `>= 0.4%` | `>= 70%` | `ALL_STOCKS` | 257 | 63.0% | `+22.26%` | **+8.81%** | 4.6% | 1.62 |
| `>= 0.8%` | `>= 30%` | `ALL_STOCKS` | 415 | 62.2% | `+29.97%` | **+8.05%** | 7.5% | 1.50 |
| `>= 0.6%` | `>= 70%` | `ALL_STOCKS` | 189 | 62.4% | `+17.01%` | **+7.27%** | 3.9% | 1.59 |
| `>= 0.8%` | `>= 60%` | `ALL_STOCKS` | 189 | 62.4% | `+17.01%` | **+7.27%** | 3.9% | 1.59 |
| `>= 1.0%` | `>= 50%` | `ALL_STOCKS` | 189 | 62.4% | `+17.01%` | **+7.27%** | 3.9% | 1.59 |
| `>= 0.6%` | `>= 40%` | `ALL_STOCKS` | 448 | 62.1% | `+30.39%` | **+6.58%** | 7.5% | 1.47 |
| `>= 0.6%` | `>= 60%` | `ALL_STOCKS` | 287 | 63.4% | `+21.66%` | **+6.58%** | 7.8% | 1.53 |

---

## 🔬 Deep Diagnostics: The 'Why' Behind the Numbers

### Diagnostic Trace for `Base Gap >= 0.4%`, `Breadth >= 50%`

**1. ALL STOCKS vs EXCLUDING WEAK STOCKS (-0.8% Gap Down)**
- 📉 **Paradox Alert:** When we EXCLUDED inherently weak stocks (<-0.8% gap down), the strategy actually LOST **-2.56%** in Net Return compared to taking all stocks.
- 💡 **The Why:** The 0 trades that occurred on these 'deep gap down' stocks actually generated a Gross PnL of **+0.00%**! These are stocks that opened in pure panic, but then staged aggressive **V-Shape Reversals** and broke out to the upside. Excluding them meant losing these massive recovery trades.

**2. ONLY TRADING GAP UP STOCKS**
- When forcing the system to ONLY trade stocks that gapped up by `>= 0.4%`, it took 415 trades for a Net Return of **-14.20%**.
- This significantly underperformed `ALL_STOCKS`. This proves that in Long Breakout trading, insisting that a stock *must* gap up restricts the universe too much. Flat or mildly down stocks often make the strongest intraday breakouts.

---
### Diagnostic Trace for `Base Gap >= 0.6%`, `Breadth >= 30%`

**1. ALL STOCKS vs EXCLUDING WEAK STOCKS (-0.8% Gap Down)**
- 📉 **Paradox Alert:** When we EXCLUDED inherently weak stocks (<-0.8% gap down), the strategy actually LOST **-2.56%** in Net Return compared to taking all stocks.
- 💡 **The Why:** The 0 trades that occurred on these 'deep gap down' stocks actually generated a Gross PnL of **+0.00%**! These are stocks that opened in pure panic, but then staged aggressive **V-Shape Reversals** and broke out to the upside. Excluding them meant losing these massive recovery trades.

**2. ONLY TRADING GAP UP STOCKS**
- When forcing the system to ONLY trade stocks that gapped up by `>= 0.6%`, it took 409 trades for a Net Return of **-11.59%**.
- This significantly underperformed `ALL_STOCKS`. This proves that in Long Breakout trading, insisting that a stock *must* gap up restricts the universe too much. Flat or mildly down stocks often make the strongest intraday breakouts.

---
### Diagnostic Trace for `Base Gap >= 0.6%`, `Breadth >= 40%`

**1. ALL STOCKS vs EXCLUDING WEAK STOCKS (-0.8% Gap Down)**
- 📉 **Paradox Alert:** When we EXCLUDED inherently weak stocks (<-0.8% gap down), the strategy actually LOST **-2.56%** in Net Return compared to taking all stocks.
- 💡 **The Why:** The 0 trades that occurred on these 'deep gap down' stocks actually generated a Gross PnL of **+0.00%**! These are stocks that opened in pure panic, but then staged aggressive **V-Shape Reversals** and broke out to the upside. Excluding them meant losing these massive recovery trades.

**2. ONLY TRADING GAP UP STOCKS**
- When forcing the system to ONLY trade stocks that gapped up by `>= 0.6%`, it took 372 trades for a Net Return of **-6.07%**.
- This significantly underperformed `ALL_STOCKS`. This proves that in Long Breakout trading, insisting that a stock *must* gap up restricts the universe too much. Flat or mildly down stocks often make the strongest intraday breakouts.

---
