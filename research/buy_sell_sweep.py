"""
FULL BUY x SELL Matrix Sweep
- 12 BUY strategies (tight, false-signal filtered)
- 10 SELL strategies (fresh, not existing system ones)
= 120 combinations
Goal: WR >= 50% AND Return >= 250%
"""
import pandas as pd
import numpy as np
import json, os
from datetime import time
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import AverageTrueRange

NIFTY_50 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK",
    "LT","HCLTECH","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","ULTRACEMCO","WIPRO","NESTLEIND",
    "TECHM","POWERGRID","NTPC","ONGC","BAJFINANCE",
    "BAJAJFINSV","ADANIENT","ADANIPORTS","DIVISLAB","DRREDDY",
    "EICHERMOT","GRASIM","HEROMOTOCO","HINDALCO","INDUSINDBK",
    "JSWSTEEL","M&M","SBILIFE","TATACONSUM","TATAMOTORS",
    "TATASTEEL","BRITANNIA","CIPLA","COALINDIA","HDFCLIFE",
    "LTIMINDTREE","BPCL","UPL","APOLLOHOSP","BAJAJ-AUTO"
]

# ─────────────────────────────────────────────
def load_and_prep(sym):
    p = f"data/cache/{sym}_60d_5m.pkl"
    if not os.path.exists(p):
        return None
    df = pd.read_pickle(p).copy()
    if len(df) < 100:
        return None
    try:
        df["hlc3"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["vwap"] = (df["hlc3"] * df["Volume"]).groupby(df.index.date).cumsum() / \
                      df["Volume"].groupby(df.index.date).cumsum()
        for w, n in [(9,"e9"),(20,"e20"),(21,"e21"),(50,"e50")]:
            df[n] = EMAIndicator(df["Close"], w).ema_indicator()
        df["rsi"]  = RSIIndicator(df["Close"], 14).rsi()
        m = MACD(df["Close"])
        df["mh"]   = m.macd_diff()
        df["msig"] = m.macd_signal()
        adx = ADXIndicator(df["High"], df["Low"], df["Close"], 14)
        df["adx"]  = adx.adx()
        df["dmp"]  = adx.adx_pos()
        df["dmn"]  = adx.adx_neg()
        df["vol20"]= df["Volume"].rolling(20).mean()
        df["h10"]  = df["High"].rolling(10).max().shift(1)
        df["h5"]   = df["High"].rolling(5).max().shift(1)
        df["l5"]   = df["Low"].rolling(5).min().shift(1)
        df["l10"]  = df["Low"].rolling(10).min().shift(1)
        atr        = AverageTrueRange(df["High"], df["Low"], df["Close"], 14)
        df["atr"]  = atr.average_true_range()
        sto        = StochasticOscillator(df["High"], df["Low"], df["Close"], 14, 3)
        df["sk"]   = sto.stoch()
        df["sd"]   = sto.stoch_signal()
        df["body"] = (df["Close"] - df["Open"]).abs()
        df["rng"]  = (df["High"] - df["Low"]).replace(0, np.nan)
        df.dropna(inplace=True)
        return df
    except Exception:
        return None

# ─────────────────────────────────────────────
# BUY SIGNAL GENERATORS (tight, high-conviction)
# ─────────────────────────────────────────────
def gen_buy(df, name):
    c,h,l,o = df["Close"], df["High"], df["Low"], df["Open"]
    v = df["vwap"]
    e9,e20,e21,e50 = df["e9"], df["e20"], df["e21"], df["e50"]
    rsi,mh = df["rsi"], df["mh"]
    adx,dmp,dmn = df["adx"], df["dmp"], df["dmn"]
    vol,v20 = df["Volume"], df["vol20"]
    h10,h5 = df["h10"], df["h5"]
    sk,sd = df["sk"], df["sd"]
    body,rng,atr = df["body"], df["rng"], df["atr"]

    # ── Tight BUY entries (multi-condition confluence) ──

    if name == "B1_ADX_TREND_SOLID":
        # ADX strong trend + DMP>DMN + solid green candle + above VWAP + 2x volume
        return (adx > 25) & (dmp > dmn) & (c > o) & (c > v) & (vol > v20 * 2.0) & (c > h10)

    elif name == "B2_EMA_STACK_VOL":
        # Full EMA stack (9>20>50) + RSI momentum sweet spot + 1.8x volume + solid candle
        return (e9 > e20) & (e20 > e50) & (rsi > 58) & (rsi < 74) & (vol > v20 * 1.8) & (c > o) & (c > h10)

    elif name == "B3_TRIPLE_CONFIRM_TIGHT":
        # RSI+MACD+ADX all aligned + 2x volume + solid candle (vs loose TRIPLE_CONFIRM)
        return (rsi > 60) & (mh > 0) & (adx > 22) & (dmp > dmn) & (vol > v20 * 2.0) & (c > o) & (c > v) & (c > h10)

    elif name == "B4_MACD_CROSS_ADX":
        # MACD hist fresh cross (momentum birth) + ADX trend + big volume + VWAP above
        cross = (mh > 0) & (mh.shift(1) <= 0)
        return cross & (adx > 20) & (dmp > dmn) & (vol > v20 * 1.8) & (c > v)

    elif name == "B5_VOL_BREAKOUT_SOLID":
        # Massive volume (2.5x) + solid body > 60% of candle range + 10-bar high + above VWAP
        big_body = body / rng > 0.6
        return (vol > v20 * 2.5) & big_body & (c > o) & (c > h10) & (c > v)

    elif name == "B6_RSI_ADX_BREAKOUT":
        # RSI in 60-74 (momentum, not overbought) + ADX>25 + EMA20>EMA50 + breakout
        return (rsi > 60) & (rsi < 74) & (adx > 25) & (e20 > e50) & (c > h10) & (vol > v20 * 1.5)

    elif name == "B7_STOCH_EMA_POWER":
        # Stoch in 40-70 (rising but not topped) + EMA stack + volume surge + solid
        stoch_ok = (sk > 40) & (sk < 70) & (sk > sd)
        return stoch_ok & (e9 > e20) & (e20 > e50) & (vol > v20 * 1.6) & (c > o) & (c > h10)

    elif name == "B8_VWAP_EMA_BREAKOUT":
        # VWAP > EMA50 (strong session) + price above all + RSI + new high
        return (v > e50) & (c > v) & (c > e20) & (rsi > 58) & (rsi < 75) & (c > h10) & (vol > v20 * 1.5) & (c > o)

    elif name == "B9_MOMENTUM_BIRTH":
        # MACD just crossed + RSI just crossed 55 + volume + VWAP reclaim
        macd_fresh = (mh > 0) & (mh.shift(1) <= 0)
        rsi_rising = (rsi > 55) & (rsi.shift(1) <= 55)
        return (macd_fresh | rsi_rising) & (c > v) & (vol > v20 * 1.8) & (c > e20)

    elif name == "B10_5MIN_POWER_CANDLE":
        # Body is >70% of ATR, very decisive candle + ADX + VWAP + volume
        power_body = body > atr * 0.7
        return power_body & (c > o) & (adx > 20) & (c > v) & (vol > v20 * 1.5) & (c > h5)

    elif name == "B11_ADX_STOCH_CLEAN":
        # ADX>25 + Stoch 35-65 (fresh momentum, not topped) + above VWAP + solid + volume
        return (adx > 25) & (sk > 35) & (sk < 65) & (c > v) & (c > o) & (vol > v20 * 1.5) & (c > h10)

    elif name == "B12_EMA_SQUEEZE_BREAK":
        # EMA9 and EMA20 were close (squeeze) then breakout with volume
        ema_gap = (e9 - e20).abs() / e20
        was_tight = ema_gap.rolling(5).min() < 0.001
        return was_tight & (e9 > e20) & (c > h10) & (vol > v20 * 2.0) & (c > o) & (c > v) & (rsi > 55)

    return pd.Series(False, index=df.index)

# ─────────────────────────────────────────────
# SELL SIGNAL GENERATORS (fresh logic, not existing strategies)
# ─────────────────────────────────────────────
def gen_sell(df, name):
    c = df["Close"]
    e9,e20,e21 = df["e9"], df["e20"], df["e21"]
    rsi, mh, msig = df["rsi"], df["mh"], df["msig"]
    v = df["vwap"]
    l5, l10 = df["l5"], df["l10"]
    sk, sd = df["sk"], df["sd"]
    atr = df["atr"]

    if name == "S1_EMA21_PREV":
        # Original Strategy 1: Close(prev) < EMA21(prev)
        return c.shift(1) < e21.shift(1)

    elif name == "S2_RSI_PEAK_DROP":
        # RSI was above 68, now crosses below → momentum peak confirmed exit
        return (rsi < 68) & (rsi.shift(1) >= 68)

    elif name == "S3_MACD_HIST_FLIP":
        # MACD histogram turns negative (momentum fading)
        return (mh < 0) & (mh.shift(1) >= 0)

    elif name == "S4_EMA9_EMA20_CROSS":
        # EMA9 crosses below EMA20 (fast momentum death signal)
        return (e9 < e20) & (e9.shift(1) >= e20.shift(1))

    elif name == "S5_MACD_AND_EMA20":
        # MACD hist flips negative AND price closes below EMA20 (double confirm)
        mh_flip = (mh < 0) & (mh.shift(1) >= 0)
        return mh_flip & (c < e20)

    elif name == "S6_VWAP_AND_EMA9":
        # Price falls below VWAP AND EMA9 < EMA20 (session momentum lost)
        return (c < v) & (e9 < e20)

    elif name == "S7_RSI_BELOW_55":
        # RSI crosses below 55 (momentum completely lost)
        return (rsi < 55) & (rsi.shift(1) >= 55)

    elif name == "S8_5BAR_LOW_BREAK":
        # Price breaks below the 5-bar low (structure breakdown)
        return c < l5

    elif name == "S9_STOCH_OVERBOUGHT_EXIT":
        # Stochastic was above 75 and crosses back down (overbought exit)
        return (sk < 75) & (sk.shift(1) >= 75) & (sk < sd)

    elif name == "S10_MACD_SIGNAL_CROSS":
        # MACD line crosses below signal line (classic bearish cross)
        return (mh < 0) & (mh.shift(1) >= 0) & (rsi < 60)

    return pd.Series(False, index=df.index)

# ─────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────
def backtest(df, bsig, ssig):
    bp = 5000.0 * 5
    in_t = False; ep = 0.0; ps = 0; hp = 0.0; tsl_p = 0.0; tsl_on = False
    trades = []
    t_ = df.index.time
    H = df["High"].values; L = df["Low"].values; C = df["Close"].values
    b = bsig.values; s = ssig.values
    mo = time(9, 15); mc = time(15, 10)
    for i in range(1, len(df)):
        ct = t_[i]
        if in_t:
            hp = max(hp, H[i])
            if hp >= ep * 1.012:
                tsl_on = True
                tsl_p = max(tsl_p, hp * 0.998)
            ex = None
            if tsl_on and L[i] <= tsl_p: ex = tsl_p
            elif s[i]:                   ex = C[i]
            elif ct >= mc:               ex = C[i]
            if ex is not None:
                trades.append((ex - ep) * ps)
                in_t = False; tsl_on = False; tsl_p = 0.0
        elif mo <= ct < mc and b[i]:
            ep = C[i]; ps = int(bp // ep)
            if ps < 1: continue
            hp = ep; tsl_p = 0.0; tsl_on = False; in_t = True
    return trades

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    print("Loading NIFTY 50 from cache...")
    stock_dfs = {}
    for sym in NIFTY_50:
        df = load_and_prep(sym)
        if df is not None:
            stock_dfs[sym] = df
    print(f"{len(stock_dfs)} stocks ready.\n")

    BUY_NAMES = [
        "B1_ADX_TREND_SOLID", "B2_EMA_STACK_VOL", "B3_TRIPLE_CONFIRM_TIGHT",
        "B4_MACD_CROSS_ADX", "B5_VOL_BREAKOUT_SOLID", "B6_RSI_ADX_BREAKOUT",
        "B7_STOCH_EMA_POWER", "B8_VWAP_EMA_BREAKOUT", "B9_MOMENTUM_BIRTH",
        "B10_5MIN_POWER_CANDLE", "B11_ADX_STOCH_CLEAN", "B12_EMA_SQUEEZE_BREAK"
    ]
    SELL_NAMES = [
        "S1_EMA21_PREV", "S2_RSI_PEAK_DROP", "S3_MACD_HIST_FLIP",
        "S4_EMA9_EMA20_CROSS", "S5_MACD_AND_EMA20", "S6_VWAP_AND_EMA9",
        "S7_RSI_BELOW_55", "S8_5BAR_LOW_BREAK", "S9_STOCH_OVERBOUGHT_EXIT",
        "S10_MACD_SIGNAL_CROSS"
    ]

    total = len(BUY_NAMES) * len(SELL_NAMES)
    print(f"Testing {total} combinations ({len(BUY_NAMES)} BUY x {len(SELL_NAMES)} SELL)...")
    print("Target: WR >= 50% AND Return >= 250%\n")

    # Pre-compute all signals for all stocks
    buy_cache  = {bn: {sym: gen_buy(df, bn)  for sym, df in stock_dfs.items()} for bn in BUY_NAMES}
    sell_cache = {sn: {sym: gen_sell(df, sn) for sym, df in stock_dfs.items()} for sn in SELL_NAMES}

    results = []
    done = 0
    for bn in BUY_NAMES:
        for sn in SELL_NAMES:
            done += 1
            all_t = []
            for sym, df in stock_dfs.items():
                all_t.extend(backtest(df, buy_cache[bn][sym], sell_cache[sn][sym]))
            if not all_t:
                continue
            wr  = len([x for x in all_t if x > 0]) / len(all_t) * 100
            ret = sum(all_t) / 5000.0 * 100
            n   = len(all_t)
            if n >= 80:  # ignore combos with too few trades
                results.append({"buy": bn, "sell": sn, "wr": round(wr, 2), "ret": round(ret, 2), "trades": n})

    # Sort: WR primary, Return secondary
    results.sort(key=lambda x: (x["wr"], x["ret"]), reverse=True)

    holy  = [r for r in results if r["wr"] >= 50.0 and r["ret"] >= 250.0]
    good  = [r for r in results if r not in holy and r["wr"] >= 46.0 and r["ret"] >= 150.0]
    rest  = [r for r in results if r not in holy and r not in good]

    print("=" * 75)
    print(f"*** HOLY GRAIL --- WR>=50% AND Ret>=250%: {len(holy)} combos found")
    print("=" * 75)
    for r in holy[:10]:
        print(f"  BUY={r['buy']}  +  SELL={r['sell']}")
        print(f"  WR={r['wr']}%  |  Ret={r['ret']}%  |  Trades={r['trades']}\n")

    print("=" * 75)
    print(f"** NEAR MISS --- WR>=46% AND Ret>=150%: {len(good)} combos")
    print("=" * 75)
    for r in good[:10]:
        print(f"  BUY={r['buy']}  +  SELL={r['sell']}")
        print(f"  WR={r['wr']}%  |  Ret={r['ret']}%  |  Trades={r['trades']}\n")

    print("=" * 75)
    print("TOP 10 BY WIN RATE (all results):")
    print("=" * 75)
    for r in results[:10]:
        flag = " ← GRAIL" if r["wr"] >= 50 and r["ret"] >= 250 else ""
        print(f"  {r['buy']:28s} + {r['sell']:25s}  WR={r['wr']}%  Ret={r['ret']}%  T={r['trades']}{flag}")

    with open("research/matrix_results.json", "w") as f:
        json.dump({"holy_grail": holy, "near_miss": good, "all": results}, f, indent=2)
    print("\nFull results -> research/matrix_results.json")

if __name__ == "__main__":
    run()
