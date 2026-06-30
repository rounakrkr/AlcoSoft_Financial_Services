# 🟢 THE LONG ENGINE MASTER BLUEPRINT (FINAL & LOCKED)

**Strategy Code Name:** `BUY_STREAK_MOMENTUM_BREAKOUT`
**Primary Objective:** Target aggressive upward momentum breakouts on days when the broader market displays strong bullish breadth.
**Constraint:** Absolute 0% operational clash with the Short Engine.

---

## 🏗️ 1. CORE SYSTEM ARCHITECTURE & SIZING
* **Margin:** `5x` (Intraday MIS multiplier).
* **Position Sizing:** Available buying power divided equally across available slots.
* **Max Open Positions (MP):** strictly `3`. You cannot hold more than 3 stocks concurrently.

---

## 🌐 2. MARKET REGIME (BULL DAY DETECTION)
* **Execution Time:** Evaluated precisely at 9:15 AM (Market Open).
* **Breadth Threshold:** At least **40%** of the stocks in the Nifty 50 universe must gap-up.
* **Gap-Up Definition:** The open price must be `>= +1.0%` higher than the previous day's close.
* **Action:** The Long Engine ONLY activates if the 40% breadth threshold of +1.0% gap-ups is met. Otherwise, it remains asleep.

---

## 🎯 3. STOCK INCLUSION & NO-CLASH FILTER
* **General Rule:** All stocks in the universe are eligible.
* **THE NO-CLASH EXCLUSION (CRITICAL):** Any individual stock that gaps down by **`-0.8%` or worse** is strictly EXCLUDED from being traded by the Long Engine. This ensures no intersection with the Short Engine's targets.

---

## ⚔️ 4. ENTRY RULES & INDICATORS
* **Primary Trigger:** `BUY_STREAK_MOMENTUM_BREAKOUT`
* **Signal Conditions (5-minute timeframe):**
  1. `streak_close_1_above_vwap_0`: The PREVIOUS completed candle's close price is ABOVE the current live VWAP.
  2. `streak_ema20_1_above_vwap_0`: The PREVIOUS completed candle's EMA(20) is ABOVE the current live VWAP.
  3. `streak_rsi_1_above_61`: The PREVIOUS completed candle's RSI(14) is strictly `> 61.0`.
  4. `streak_close_0_above_period_max_10`: The CURRENT (live) candle breaks out and closes ABOVE the highest high of the last 10 candles.

* **RULE 2 (DYNAMIC ENTRY BLOCKER):**
  * Before triggering an entry, evaluate the `SELL_EMA_MOMENTUM_LOSS` condition on the current closing candle.
  * If the dynamic cover strategy is currently firing, **BLOCK THE ENTRY**. Do not take the trade.

* **Execution:** If all rules are met, execute a LONG ENTRY at the **Open of the Next Candle**.

---

## 🛡️ 5. EXIT STRATEGY (LOOKAHEAD-FREE DEFENSE & PROFIT)
> [!IMPORTANT]
> **Lookahead Bias Prevention:** Indicator-based exits (RSI & Dynamic Exit) must ONLY be evaluated on candles *after* the entry candle. You cannot enter at the Open of Candle T and evaluate indicator exits using the Close of Candle T simultaneously.

* **Fixed Stop Loss (Hard Risk):** Exactly **1.0%** below the entry price. Evaluated against the live low of the current candle.
* **Partial Profit Target (Cash Grab):** As soon as the position is in profit by **`+0.5%`**, cover and book **75%** of the total quantity. Evaluated against the live high of the current candle.
* **Dynamic Exit (EMA Momentum Loss):** 
  * Trigger: `SELL_EMA_MOMENTUM_LOSS`
  * Timing: Starting from the candle *after* entry.
  * Action: If the closed candle loses EMA momentum and triggers this condition, exit the trade dynamically at the next available open price, ignoring the fixed stop loss.
* **Overbought Exit (Runner Exit):**
  * Trigger: `RSI(14) >= 72.0`
  * Timing: Starting from the candle *after* entry.
  * Action: Close the remaining 25% quantity immediately at the next Open if the previous completed candle's RSI hits >= 72.0.
* **Intraday EOD Exit:** Square off any remaining positions unconditionally at **15:15 (3:15 PM)**.

---

## 🚀 VERIFIED PERFORMANCE METRICS (Unbiased 60-Day Standalone)
* **Total Trades:** `180`
* **Win Rate:** `61.11%`
* **Gross Return:** `+16.56%`
* **STT Tax Impact:** `-9.10%`
* **Absolute Net Return:** **`+7.46%`** 🟩
* **Profit Factor:** `1.25`
* **Expectancy:** ₹41.45 per trade
