"""
MIDCAP50 LIVE-ENGINE REPLICA BACKTESTER + OPTIMIZER
Replicates the LIVE AlcoSoft engine config exactly:
  Long : BUY_R7_VARIANT_D            (bull regime days only)
  Short: SHORT_STREAK_MOMENTUM_BREAKDOWN (bear regime days only)
Exits: fixed SL, partial profit, RSI exit, EMA50 dyn exit (min-hold), TSL, 15:15 EOD.
Costs: real Kotak zero-brokerage cost model (STT/NSE/SEBI/GST/stamp).
"""
import os, sys, glob, pickle, itertools, json
import numpy as np
import pandas as pd

DATA_DIR = "/tmp/midcap50_data/midcap50_5min_history"
CACHE = "/app/research/mc50_arrays.pkl"

# ── cost model (mirror of core.state_manager.calculate_transaction_costs) ──
def txn_costs(entry, exit_, qty):
    buy_val, sell_val = entry * qty, exit_ * qty
    turnover = buy_val + sell_val
    stt = sell_val * 0.00025
    nse = turnover * 0.0000297
    sebi = turnover * 0.000001
    gst = (nse + sebi) * 0.18
    stamp = buy_val * 0.00003
    return stt + nse + sebi + gst + stamp

# ── indicators (mirror of core.strategy._build_indicators) ──
def build_symbol(fp):
    df = pd.read_csv(fp)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df.dropna(subset=["close"])
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    # RSI14 (Wilder, same as ta lib)
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn)
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    tp = (h + l + c) / 3
    day = df.index.date
    df["vwap"] = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    return df

def load_all():
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    out = {}
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, "*_5min.csv"))):
        sym = os.path.basename(fp).replace("_5min.csv", "")
        out[sym] = build_symbol(fp)
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    return out

# ── per-symbol precomputation: signals + per-day row ranges ──
def precompute(dfs):
    P = {}
    for sym, df in dfs.items():
        n = len(df)
        c = df["close"].values; o = df["open"].values
        h = df["high"].values; l = df["low"].values
        rsi = df["rsi"].values; e20 = df["ema20"].values
        e50 = df["ema50"].values; vw = df["vwap"].values; macd = df["macd"].values
        c1 = np.roll(c, 1); e20_1 = np.roll(e20, 1); rsi1 = np.roll(rsi, 1); h1 = np.roll(h, 1)
        # rolling min of low over 10 candles ending at prev candle
        low10 = pd.Series(l).rolling(10).min().shift(1).values
        long_sig = (c1 > vw) & (e20_1 > vw) & (rsi1 > 61) & (macd > 0)
        short_sig = (c1 < vw) & (e20_1 < vw) & (rsi1 < 39) & (c < low10) & (c <= h1) & (c >= vw * 0.988)
        # day boundaries: invalidate cross-day lag conditions on first candle of day
        dates = np.array(df.index.date)
        first_of_day = np.ones(n, dtype=bool)
        first_of_day[1:] = dates[1:] != dates[:-1]
        long_sig &= ~first_of_day
        short_sig &= ~first_of_day
        minutes = df.index.hour * 60 + df.index.minute
        P[sym] = dict(o=o, h=h, l=l, c=c, rsi=rsi, e50=e50,
                      long=long_sig.astype(bool), short=short_sig.astype(bool),
                      dates=dates, minutes=minutes.values, index=df.index)
    return P

# ── daily gaps + regime days ──
def compute_gaps(dfs):
    opens, closes = {}, {}
    for sym, df in dfs.items():
        g = df.groupby(df.index.date)
        opens[sym] = g["open"].first()
        closes[sym] = g["close"].last().shift(1)
    gap = {}
    for sym in dfs:
        gp = (opens[sym] - closes[sym]) / closes[sym]
        for d, val in gp.items():
            if pd.notna(val):
                gap[(d, sym)] = float(val)
    all_days = sorted({d for d, s in gap})
    return gap, all_days

def regime_days(gap, all_days, syms, bull_gap, bull_br, bear_gap, bear_br):
    bull, bear = set(), set()
    for d in all_days:
        gs = [gap[(d, s)] for s in syms if (d, s) in gap]
        if not gs:
            continue
        if sum(1 for g in gs if g >= bull_gap) / len(gs) >= bull_br:
            bull.add(d)
        if sum(1 for g in gs if g <= bear_gap) / len(gs) >= bear_br:
            bear.add(d)
    bear -= bull & bear  # live: if both, prefer bear=False? regime_filter prefers bear... 
    return bull, bear

# live regime_filter: "if is_bull and is_bear: is_bear = False" → prefer BULL
def regime_days_live(gap, all_days, syms, bull_gap, bull_br, bear_gap, bear_br):
    bull, bear = set(), set()
    for d in all_days:
        gs = [gap[(d, s)] for s in syms if (d, s) in gap]
        if not gs:
            continue
        b = sum(1 for g in gs if g >= bull_gap) / len(gs) >= bull_br
        r = sum(1 for g in gs if g <= bear_gap) / len(gs) >= bear_br
        if b and r:
            r = False
        if b: bull.add(d)
        if r: bear.add(d)
    return bull, bear

# ── day simulation (portfolio, max_open_positions slots) ──
def run(P, gap, days, params, capital0=100000.0, date_from=None, date_to=None):
    p = params
    trades = []
    capital = capital0
    day_rows = {}  # (sym, day) -> (start, end) precomputed lazily
    for sym, d in P.items():
        dates = d["dates"]
        # build day -> slice map once per symbol
        change = np.nonzero(np.concatenate(([True], dates[1:] != dates[:-1])))[0]
        ends = np.concatenate((change[1:], [len(dates)]))
        d["_daymap"] = {dates[s]: (s, e) for s, e in zip(change, ends)}

    for day in days:
        if date_from and day < date_from: continue
        if date_to and day > date_to: continue
        engine = day[1]  # ('date', 'LONG'/'SHORT')
        d0 = day[0]
        # eligible symbols with gap filter
        elig = []
        for sym in P:
            g = gap.get((d0, sym))
            if g is None: continue
            if engine == "LONG" and g <= p["long_exclude_gap"]: continue
            if engine == "SHORT" and g > p["short_target_gap"]: continue
            if d0 not in P[sym]["_daymap"]: continue
            elig.append(sym)
        if not elig: continue

        open_pos = []   # list of dicts
        slots = p["maxpos"]
        # candle times are aligned across symbols (5-min grid); iterate minutes 9:15..15:25
        # gather per-symbol day slices
        sl_map = {s: P[s]["_daymap"][d0] for s in elig}
        # build global minute grid for this day
        minute_set = set()
        for s in elig:
            st, en = sl_map[s]
            minute_set.update(P[s]["minutes"][st:en])
        grid = sorted(minute_set)
        # index per symbol per minute
        idx_of = {}
        for s in elig:
            st, en = sl_map[s]
            mm = P[s]["minutes"][st:en]
            idx_of[s] = {int(m): st + k for k, m in enumerate(mm)}

        pending_entries = []  # signals fired at candle t -> execute at open of t+1
        pending_exits = []    # (pos, reason) close-based exits -> next open

        for mi, minute in enumerate(grid):
            nxt = grid[mi + 1] if mi + 1 < len(grid) else None
            # 1) execute pending exits at THIS candle open
            for pos, reason in pending_exits:
                i = idx_of[pos["sym"]].get(minute)
                if i is None or pos["qty"] <= 0: continue
                px = P[pos["sym"]]["o"][i]
                close_trade(trades, pos, px, pos["qty"], reason, P[pos["sym"]]["index"][i])
            pending_exits = []
            open_pos = [x for x in open_pos if x["qty"] > 0]
            # 2) execute pending entries at THIS candle open
            for sym in pending_entries:
                if len(open_pos) >= slots: break
                if any(x["sym"] == sym for x in open_pos): continue
                i = idx_of[sym].get(minute)
                if i is None: continue
                px = P[sym]["o"][i]
                if px <= 0: continue
                alloc = (capital * p["margin"]) / slots
                qty = int(alloc // px)
                if qty <= 0: continue
                slp = p["long_sl"] if engine == "LONG" else p["short_sl"]
                sl_price = px * (1 - slp) if engine == "LONG" else px * (1 + slp)
                open_pos.append(dict(sym=sym, dir=engine, entry=px, qty=qty, qty0=qty,
                                     sl=sl_price, sl_pct=slp, tsl=None, tsl_on=False,
                                     partial_done=False, rsi_done=False, entry_i=i,
                                     entry_t=P[sym]["index"][i], candles=0))
            pending_entries = []

            # 3) manage open positions on this candle
            for pos in open_pos:
                sym = pos["sym"]; i = idx_of[sym].get(minute)
                if i is None or pos["qty"] <= 0: continue
                if i == pos["entry_i"]:
                    # entry candle: only hard SL monitored intra-candle
                    if hit_sl(pos, P[sym], i, trades): continue
                    continue
                pos["candles"] += 1
                dd = P[sym]
                # a) hard SL / TSL first (intra-candle)
                if hit_sl(pos, dd, i, trades): continue
                # b) partial profit target (intra-candle trigger price)
                if p["pp_enabled"] and not pos["partial_done"]:
                    tgt = p["long_pt"] if pos["dir"] == "LONG" else p["short_pt"]
                    frac = p["long_pp_frac"] if pos["dir"] == "LONG" else p["short_pp_frac"]
                    if pos["dir"] == "LONG":
                        trig = pos["entry"] * (1 + tgt)
                        hit = dd["h"][i] >= trig
                    else:
                        trig = pos["entry"] * (1 - tgt)
                        hit = dd["l"][i] <= trig
                    if hit:
                        pos["partial_done"] = True
                        q = max(1, int(pos["qty"] * frac))
                        close_trade(trades, pos, trig, q, "PARTIAL_PROFIT", dd["index"][i])
                        if pos["qty"] <= 0: continue
                # c) TSL activation & trail (close-based, mirror live loop cadence)
                cpx = dd["c"][i]
                if not pos["tsl_on"]:
                    act = pos["entry"] * (1 + pos["sl_pct"] * p["tsl_act"]) if pos["dir"] == "LONG" \
                        else pos["entry"] * (1 - pos["sl_pct"] * p["tsl_act"])
                    if (pos["dir"] == "LONG" and cpx >= act) or (pos["dir"] == "SHORT" and cpx <= act):
                        pos["tsl_on"] = True
                if pos["tsl_on"]:
                    t = cpx * (1 - p["tsl_pct"]) if pos["dir"] == "LONG" else cpx * (1 + p["tsl_pct"])
                    if pos["tsl"] is None or (pos["dir"] == "LONG" and t > pos["tsl"]) or \
                       (pos["dir"] == "SHORT" and t < pos["tsl"]):
                        pos["tsl"] = t
                # d) RSI exit (close-based → next open)
                if p["rsi_exit"] and not pos["rsi_done"]:
                    r = dd["rsi"][i]
                    if not np.isnan(r):
                        if (pos["dir"] == "LONG" and r >= p["long_rsi"]) or \
                           (pos["dir"] == "SHORT" and r <= p["short_rsi"]):
                            pos["rsi_done"] = True
                            pending_exits.append((pos, "RSI_EXIT"))
                            continue
                # e) EMA50 dynamic exit for LONG (close-based, min-hold) → next open
                if pos["dir"] == "LONG" and p["ema50_exit"] and pos["candles"] >= p["min_hold"]:
                    if cpx < dd["e50"][i]:
                        pending_exits.append((pos, "EMA50_DYN"))
                        continue
                # f) EOD squareoff at 15:15 (minute 915)
                if minute >= 915 or nxt is None:
                    close_trade(trades, pos, cpx, pos["qty"], "EOD", dd["index"][i])

            open_pos = [x for x in open_pos if x["qty"] > 0]

            # 4) detect new signals on this candle (execute next open)
            if minute < 900 and len(open_pos) < slots:  # no new trades >= 15:00
                key = "long" if engine == "LONG" else "short"
                for sym in elig:
                    if any(x["sym"] == sym for x in open_pos): continue
                    i = idx_of[sym].get(minute)
                    if i is None: continue
                    if P[sym][key][i]:
                        pending_entries.append(sym)

        # force close leftovers at last candle close
        for pos in open_pos:
            if pos["qty"] > 0:
                st, en = sl_map[pos["sym"]]
                close_trade(trades, pos, P[pos["sym"]]["c"][en-1], pos["qty"], "EOD_FORCE",
                            P[pos["sym"]]["index"][en-1])
        # compound capital daily
        day_pnl = sum(t["net"] for t in trades if t["exit_t"].date() == d0)
        capital += day_pnl
        if capital <= 0:
            break
    return trades, capital

def hit_sl(pos, dd, i, trades):
    stop = pos["tsl"] if (pos["tsl_on"] and pos["tsl"] is not None) else pos["sl"]
    if pos["dir"] == "LONG":
        if dd["l"][i] <= stop:
            px = min(stop, dd["o"][i]) if dd["o"][i] < stop else stop
            close_trade(trades, pos, px, pos["qty"], "SL" if not pos["tsl_on"] else "TSL", dd["index"][i])
            return True
    else:
        if dd["h"][i] >= stop:
            px = max(stop, dd["o"][i]) if dd["o"][i] > stop else stop
            close_trade(trades, pos, px, pos["qty"], "SL" if not pos["tsl_on"] else "TSL", dd["index"][i])
            return True
    return False

def close_trade(trades, pos, px, qty, reason, ts):
    qty = min(qty, pos["qty"])
    if qty <= 0: return
    gross = (px - pos["entry"]) * qty if pos["dir"] == "LONG" else (pos["entry"] - px) * qty
    net = gross - txn_costs(pos["entry"], px, qty)
    trades.append(dict(sym=pos["sym"], dir=pos["dir"], entry=pos["entry"], exit=px, qty=qty,
                       gross=gross, net=net, reason=reason, entry_t=pos["entry_t"], exit_t=ts))
    pos["qty"] -= qty

# ── metrics ──
def metrics(trades, capital0=100000.0):
    if not trades:
        return dict(n=0, net=0, ret=0, wr=0, pf=0, maxdd=0)
    df = pd.DataFrame(trades)
    df = df.sort_values("exit_t")
    net = df["net"].sum()
    wins = df[df["net"] > 0]["net"].sum()
    losses = -df[df["net"] <= 0]["net"].sum()
    daily = df.groupby(df["exit_t"].dt.date)["net"].sum()
    eq = capital0 + daily.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return dict(n=len(df), net=round(net), ret=round(100 * net / capital0, 1),
                wr=round(100 * (df["net"] > 0).mean(), 1),
                pf=round(wins / losses, 2) if losses > 0 else 99,
                maxdd=round(100 * dd, 1),
                by_reason=df.groupby("reason")["net"].agg(["count", "sum"]).round(0).to_dict("index"))

LIVE = dict(margin=5.0, maxpos=1,
            long_sl=0.01, short_sl=0.005, long_pt=0.025, short_pt=0.025,
            pp_enabled=True, long_pp_frac=0.25, short_pp_frac=1.0,
            rsi_exit=True, long_rsi=78.0, short_rsi=17.0,
            ema50_exit=True, min_hold=4,
            tsl_act=1.4, tsl_pct=0.008,
            long_exclude_gap=-0.008, short_target_gap=-0.015,
            bull_gap=0.007, bull_br=0.35, bear_gap=-0.006, bear_br=0.40)

if __name__ == "__main__":
    dfs = load_all()
    print(f"Loaded {len(dfs)} symbols")
    P = precompute(dfs)
    gap, all_days = compute_gaps(dfs)
    syms = list(dfs.keys())
    bull, bear = regime_days_live(gap, all_days, syms, LIVE["bull_gap"], LIVE["bull_br"],
                                  LIVE["bear_gap"], LIVE["bear_br"])
    print(f"Days: {len(all_days)} | Bull: {len(bull)} | Bear: {len(bear)}")
    days = sorted([(d, "LONG") for d in bull] + [(d, "SHORT") for d in bear])
    trades, cap = run(P, gap, days, LIVE)
    m = metrics(trades)
    print(json.dumps(m, indent=2, default=str))
    print(f"Final capital: {cap:,.0f}")
    pd.DataFrame(trades).to_csv("/app/research/mc50_baseline_trades.csv", index=False)
