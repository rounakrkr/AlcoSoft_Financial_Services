# 🔴 THE SHORT ENGINE MASTER BLUEPRINT (FINAL & LOCKED)

**Strategy Code Name:** `SHORT_STREAK_MOMENTUM_BREAKDOWN`
**Primary Objective:** Target panic sell-offs during extreme bear market conditions using momentum breakdown triggers.

---

## 🏗️ 1. CORE SYSTEM ARCHITECTURE & SIZING
* **Margin:** `5x` (Intraday MIS multiplier).
* **Position Sizing:** Available buying power divided equally across available slots.
* **Max Open Positions (MP):** strictly `3`. You cannot hold more than 3 stocks concurrently.

---

## 🌐 2. MARKET REGIME (BEAR DAY DETECTION)
* **Execution Time:** Evaluated precisely at 9:15 AM (Market Open).
* **Breadth Threshold:** At least **40%** of the stocks in the Nifty 50 universe must gap-down.
* **Gap-Down Definition:** The open price must be `<= -0.6%` lower than the previous day's close.
* **Action:** The Short Engine ONLY activates if the 40% breadth threshold of -0.6% gap-downs is met. Otherwise, it remains asleep.

---

## 🎯 3. STOCK INCLUSION FILTER
* **General Rule:** On a confirmed Bear Day, we apply a strict stock-level filter.
* **Condition:** Only stocks that individually gapped down by **`-0.8%` or worse** are eligible for shorting. All other stocks are ignored. This strictly isolates the weakest stocks for shorting.

---

## ⚔️ 4. ENTRY RULES & INDICATORS
* **Primary Trigger:** `SHORT_STREAK_MOMENTUM_BREAKDOWN`
* **Signal Conditions (5-minute timeframe):**
  1. `streak_close_1_below_vwap_0`: The PREVIOUS completed candle's close price is BELOW the current live VWAP.
  2. `streak_ema20_1_below_vwap_0`: The PREVIOUS completed candle's EMA(20) is BELOW the current live VWAP.
  3. `streak_rsi_1_below_39`: The PREVIOUS completed candle's RSI(14) is strictly `< 39.0`.
  4. `streak_close_0_below_period_min_10`: The CURRENT (live) candle breaks down and closes BELOW the lowest low of the last 10 candles.

* **RULE 2 (DYNAMIC ENTRY BLOCKER):**
  * Before triggering an entry, evaluate the `SHORT_STREAK_MOMENTUM_RECOVERY` condition (specifically checking if yesterday's close is greater than the day-before-yesterday's high).
  * If the dynamic cover strategy is currently firing, **BLOCK THE ENTRY**. Do not take the trade.

* **Execution:** If all rules are met, execute a SHORT ENTRY at the **Open of the Next Candle**.

---

## 🛡️ 5. EXIT STRATEGY (LOOKAHEAD-FREE DEFENSE & PROFIT)
> [!IMPORTANT]
> **Lookahead Bias Prevention:** Indicator-based exits (RSI & Dynamic SL) must ONLY be evaluated on candles *after* the entry candle. You cannot enter at the Open of Candle T and evaluate indicator exits using the Close of Candle T simultaneously.

* **Fixed Stop Loss:** There is **NO** fixed percentage Stop-Loss for the short engine.
* **Partial Profit Target (Cash Grab):** As soon as the position is in profit by **`+0.5%`**, immediately cover and book **75%** of the total quantity. Evaluated against the live low of the current candle.
* **Dynamic Stop-Loss (Lag-1):** 
  * Trigger: If Yesterday's Close is strictly greater than the Day-Before-Yesterday's High (`c1 > h2`).
  * Timing: Starting from the candle *after* entry.
  * Action: Exit the entire position at the next open.
* **Extreme Panic Exit (Runner Exit):**
  * Trigger: `RSI(16) <= 15.0`
  * Timing: Starting from the candle *after* entry.
  * Action: Close the remaining 25% quantity immediately at the next Open if the previous completed candle's RSI hits <= 15.0.
* **Intraday EOD Exit:** Square off any remaining positions unconditionally at **15:15 (3:15 PM)**.

---

## 🚀 VERIFIED PERFORMANCE METRICS (Unbiased 60-Day Standalone)
* **Total Trades:** `178`
* **Win Rate:** `51.12%`
* **Gross Return:** `+14.09%`
* **STT Tax Impact:** `-8.21%`
* **Absolute Net Return:** **`+5.87%`** 🟩
* **Profit Factor:** `1.22`
* **Expectancy:** ₹32.98 per trade
