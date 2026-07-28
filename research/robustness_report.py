"""
COMPREHENSIVE ROBUSTNESS ANALYSIS for the OPTIMIZED Midcap50 config.
Generates all 6 sections requested (metrics, walk-forward, top-5 removal, no-leverage,
EMA50 alternative, short-only per-year) into a single markdown report.

Baseline config = the OPTIMIZED live config (as committed to trading_settings.json):
  long_sl 1.2%, long_pt 3.5%, long_pp_frac 0.75, RSI exit OFF, TSL 1.2x/0.5%
  short_sl 0.5%, short_pt 2%, short_target_gap -2.5%
  bull_gap 0.7%/breadth 35%, bear_gap -0.6%/breadth 40%
  max_open_positions 1, r7_min_hold 4, long_exclude_gap -0.8%, 5x margin, ₹1L start, daily compound
"""
import sys, os, json, math, datetime, itertools
import numpy as np
import pandas as pd

sys.path.insert(0, "/app/research")
from midcap50_optimizer import (
    load_all, precompute, compute_gaps, regime_days_live, run, metrics, LIVE, txn_costs,
)

# ── OPTIMIZED CONFIG (final, applied to trading_settings.json) ──
OPT = dict(
    LIVE,
    long_sl=0.012, long_pt=0.035, long_pp_frac=0.75,
    rsi_exit=False, min_hold=4,
    tsl_act=1.2, tsl_pct=0.005,
    short_sl=0.005, short_pt=0.02, short_target_gap=-0.025,
    long_exclude_gap=-0.008, maxpos=1,
    margin=5.0,
    ema50_exit=True,
    time_exit_candles=0,  # 0 = disabled; only used in EMA50 alt test
)

OUT = "/app/research/ROBUSTNESS_REPORT.md"
DATA_PKL = "/app/research/robustness_data.pkl"


# ── HELPERS ───────────────────────────────────────────────────────────
def to_df(trades):
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades).sort_values("exit_t").reset_index(drop=True)
    df["exit_date"] = pd.to_datetime(df["exit_t"]).dt.date
    df["year"] = pd.to_datetime(df["exit_t"]).dt.year
    df["month"] = pd.to_datetime(df["exit_t"]).dt.to_period("M").astype(str)
    return df


def daily_pnl(df, capital0):
    if df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    daily = df.groupby("exit_date")["net"].sum().sort_index()
    daily.index = pd.to_datetime(daily.index)
    return daily, capital0 + daily.cumsum()


def full_metrics(df, capital0=100000.0, label=""):
    """Compute complete metrics dict."""
    if df.empty:
        return dict(label=label, n=0)
    net = df["net"].sum()
    wins = df[df["net"] > 0]
    losses = df[df["net"] <= 0]
    daily, eq = daily_pnl(df, capital0)
    # daily returns for Sharpe: PnL / equity at start of day
    prev_eq = eq.shift(1).fillna(capital0)
    daily_ret = daily / prev_eq
    sharpe = 0.0
    if daily_ret.std() > 0:
        sharpe = (daily_ret.mean() / daily_ret.std()) * math.sqrt(252)
    # max drawdown
    dd = ((eq - eq.cummax()) / eq.cummax()).min() if not eq.empty else 0
    # max losing streak (trade-by-trade)
    seq = (df["net"] <= 0).astype(int).tolist()
    max_streak, cur = 0, 0
    for x in seq:
        cur = cur + 1 if x else 0
        max_streak = max(max_streak, cur)
    # monthly returns
    monthly = df.groupby("month")["net"].sum()
    neg_months = int((monthly <= 0).sum())
    tot_months = int(len(monthly))
    return dict(
        label=label,
        n=int(len(df)),
        net=round(float(net)),
        ret_pct=round(100 * net / capital0, 1),
        wr=round(100 * len(wins) / len(df), 1),
        avg_win=round(float(wins["net"].mean()) if len(wins) else 0, 0),
        avg_loss=round(float(losses["net"].mean()) if len(losses) else 0, 0),
        pf=round(wins["net"].sum() / -losses["net"].sum(), 2) if len(losses) and losses["net"].sum() < 0 else 99,
        maxdd=round(100 * dd, 1),
        sharpe=round(sharpe, 2),
        max_loss_streak=max_streak,
        months_total=tot_months,
        months_negative=neg_months,
        pct_months_negative=round(100 * neg_months / tot_months, 1) if tot_months else 0,
    )


def per_year_table(df, capital0=100000.0):
    """Year -> {trades, wr, ret%, pf, maxdd}."""
    if df.empty:
        return pd.DataFrame()
    rows = []
    # Capital at start of each year (using compounded equity)
    daily, eq = daily_pnl(df, capital0)
    for yr, g in df.groupby("year"):
        wins = g[g["net"] > 0]["net"].sum()
        losses = -g[g["net"] <= 0]["net"].sum()
        # cap at year start = capital0 + cumulative pnl up to Dec 31 (yr-1)
        prev_end = pd.Timestamp(f"{yr - 1}-12-31")
        cap_start = capital0 + daily[daily.index <= prev_end].sum()
        # DD within year
        yr_daily = daily[daily.index.year == yr]
        yr_eq = cap_start + yr_daily.cumsum()
        yr_dd = ((yr_eq - yr_eq.cummax()) / yr_eq.cummax()).min() if not yr_eq.empty else 0
        rows.append(dict(
            year=int(yr),
            trades=int(len(g)),
            wr=round(100 * (g["net"] > 0).mean(), 1),
            net=round(g["net"].sum()),
            ret_pct=round(100 * g["net"].sum() / cap_start, 1) if cap_start > 0 else 0,
            pf=round(wins / losses, 2) if losses > 0 else 99,
            maxdd=round(100 * yr_dd, 1),
        ))
    return pd.DataFrame(rows)


def per_year_direction(df, direction, capital0=100000.0):
    if df.empty:
        return pd.DataFrame()
    sub = df[df["dir"] == direction].copy()
    return per_year_table(sub, capital0=capital0) if not sub.empty else pd.DataFrame()


def month_end_equity(df, capital0=100000.0):
    if df.empty:
        return pd.DataFrame()
    daily, eq = daily_pnl(df, capital0)
    if eq.empty:
        return pd.DataFrame()
    # last equity value per month
    m = eq.groupby(eq.index.to_period("M")).last()
    m.index = m.index.astype(str)
    monthly_ret = m.pct_change().fillna(m.iloc[0] / capital0 - 1) * 100
    return pd.DataFrame({"equity": m.round(0), "month_ret_pct": monthly_ret.round(1)})


# ── SECTION 5 helper: modified run() with EMA50 -> time-based exit ──
def run_with_time_exit(P, gap, days, params, time_exit_candles, capital0=100000.0):
    """Runs live-engine backtest but replaces EMA50_DYN with a candle-count exit.
    Implementation: same as run() but sets ema50_exit=False and after `time_exit_candles`
    candles held, force close at next-open with reason TIME_EXIT.
    """
    p = dict(params, ema50_exit=False)
    # Precompute day slices
    for sym, d in P.items():
        dates = d["dates"]
        if "_daymap" not in d:
            change = np.nonzero(np.concatenate(([True], dates[1:] != dates[:-1])))[0]
            ends = np.concatenate((change[1:], [len(dates)]))
            d["_daymap"] = {dates[s]: (s, e) for s, e in zip(change, ends)}
    trades = []
    capital = capital0
    for day in days:
        d0 = day[0]; engine = day[1]
        elig = []
        for sym in P:
            g = gap.get((d0, sym))
            if g is None: continue
            if engine == "LONG" and g <= p["long_exclude_gap"]: continue
            if engine == "SHORT" and g > p["short_target_gap"]: continue
            if d0 not in P[sym]["_daymap"]: continue
            elig.append(sym)
        if not elig: continue

        open_pos = []
        slots = p["maxpos"]
        sl_map = {s: P[s]["_daymap"][d0] for s in elig}
        minute_set = set()
        for s in elig:
            st, en = sl_map[s]
            minute_set.update(P[s]["minutes"][st:en])
        grid = sorted(minute_set)
        idx_of = {}
        for s in elig:
            st, en = sl_map[s]
            mm = P[s]["minutes"][st:en]
            idx_of[s] = {int(m): st + k for k, m in enumerate(mm)}

        pending_entries = []
        pending_exits = []

        for mi, minute in enumerate(grid):
            nxt = grid[mi + 1] if mi + 1 < len(grid) else None
            for pos, reason in pending_exits:
                i = idx_of[pos["sym"]].get(minute)
                if i is None or pos["qty"] <= 0: continue
                px = P[pos["sym"]]["o"][i]
                _close(trades, pos, px, pos["qty"], reason, P[pos["sym"]]["index"][i])
            pending_exits = []
            open_pos = [x for x in open_pos if x["qty"] > 0]
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

            for pos in open_pos:
                sym = pos["sym"]; i = idx_of[sym].get(minute)
                if i is None or pos["qty"] <= 0: continue
                if i == pos["entry_i"]:
                    _hit_sl(pos, P[sym], i, trades)
                    continue
                pos["candles"] += 1
                dd = P[sym]
                if _hit_sl(pos, dd, i, trades): continue
                if p["pp_enabled"] and not pos["partial_done"]:
                    tgt = p["long_pt"] if pos["dir"] == "LONG" else p["short_pt"]
                    frac = p["long_pp_frac"] if pos["dir"] == "LONG" else p["short_pp_frac"]
                    if pos["dir"] == "LONG":
                        trig = pos["entry"] * (1 + tgt); hit = dd["h"][i] >= trig
                    else:
                        trig = pos["entry"] * (1 - tgt); hit = dd["l"][i] <= trig
                    if hit:
                        pos["partial_done"] = True
                        q = max(1, int(pos["qty"] * frac))
                        _close(trades, pos, trig, q, "PARTIAL_PROFIT", dd["index"][i])
                        if pos["qty"] <= 0: continue
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
                # TIME EXIT (replaces EMA50_DYN)
                if pos["dir"] == "LONG" and pos["candles"] >= time_exit_candles:
                    pending_exits.append((pos, "TIME_EXIT"))
                    continue
                if minute >= 915 or nxt is None:
                    _close(trades, pos, cpx, pos["qty"], "EOD", dd["index"][i])

            open_pos = [x for x in open_pos if x["qty"] > 0]
            if minute < 900 and len(open_pos) < slots:
                key = "long" if engine == "LONG" else "short"
                for sym in elig:
                    if any(x["sym"] == sym for x in open_pos): continue
                    i = idx_of[sym].get(minute)
                    if i is None: continue
                    if P[sym][key][i]:
                        pending_entries.append(sym)

        for pos in open_pos:
            if pos["qty"] > 0:
                st, en = sl_map[pos["sym"]]
                _close(trades, pos, P[pos["sym"]]["c"][en - 1], pos["qty"], "EOD_FORCE",
                       P[pos["sym"]]["index"][en - 1])
        day_pnl_val = sum(t["net"] for t in trades if t["exit_t"].date() == d0)
        capital += day_pnl_val
        if capital <= 0: break
    return trades, capital


def _close(trades, pos, px, qty, reason, ts):
    qty = min(qty, pos["qty"])
    if qty <= 0: return
    gross = (px - pos["entry"]) * qty if pos["dir"] == "LONG" else (pos["entry"] - px) * qty
    net = gross - txn_costs(pos["entry"], px, qty)
    trades.append(dict(sym=pos["sym"], dir=pos["dir"], entry=pos["entry"], exit=px, qty=qty,
                       gross=gross, net=net, reason=reason, entry_t=pos["entry_t"], exit_t=ts))
    pos["qty"] -= qty


def _hit_sl(pos, dd, i, trades):
    stop = pos["tsl"] if (pos["tsl_on"] and pos["tsl"] is not None) else pos["sl"]
    if pos["dir"] == "LONG":
        if dd["l"][i] <= stop:
            px = min(stop, dd["o"][i]) if dd["o"][i] < stop else stop
            _close(trades, pos, px, pos["qty"], "SL" if not pos["tsl_on"] else "TSL", dd["index"][i])
            return True
    else:
        if dd["h"][i] >= stop:
            px = max(stop, dd["o"][i]) if dd["o"][i] > stop else stop
            _close(trades, pos, px, pos["qty"], "SL" if not pos["tsl_on"] else "TSL", dd["index"][i])
            return True
    return False


# ── SECTION 4 helper: fixed-capital, no-compounding run ──
def run_no_compound(P, gap, days, params, capital_per_trade=100000.0):
    """Fixed ₹1L per trade allocation, 1x margin, NO compounding. Uses same live engine
    but overrides alloc computation."""
    p = dict(params, margin=1.0)
    for sym, d in P.items():
        dates = d["dates"]
        if "_daymap" not in d:
            change = np.nonzero(np.concatenate(([True], dates[1:] != dates[:-1])))[0]
            ends = np.concatenate((change[1:], [len(dates)]))
            d["_daymap"] = {dates[s]: (s, e) for s, e in zip(change, ends)}
    trades = []
    for day in days:
        d0 = day[0]; engine = day[1]
        elig = []
        for sym in P:
            g = gap.get((d0, sym))
            if g is None: continue
            if engine == "LONG" and g <= p["long_exclude_gap"]: continue
            if engine == "SHORT" and g > p["short_target_gap"]: continue
            if d0 not in P[sym]["_daymap"]: continue
            elig.append(sym)
        if not elig: continue
        open_pos = []
        slots = p["maxpos"]
        sl_map = {s: P[s]["_daymap"][d0] for s in elig}
        minute_set = set()
        for s in elig:
            st, en = sl_map[s]
            minute_set.update(P[s]["minutes"][st:en])
        grid = sorted(minute_set)
        idx_of = {}
        for s in elig:
            st, en = sl_map[s]
            mm = P[s]["minutes"][st:en]
            idx_of[s] = {int(m): st + k for k, m in enumerate(mm)}
        pending_entries = []; pending_exits = []
        for mi, minute in enumerate(grid):
            nxt = grid[mi + 1] if mi + 1 < len(grid) else None
            for pos, reason in pending_exits:
                i = idx_of[pos["sym"]].get(minute)
                if i is None or pos["qty"] <= 0: continue
                px = P[pos["sym"]]["o"][i]
                _close(trades, pos, px, pos["qty"], reason, P[pos["sym"]]["index"][i])
            pending_exits = []
            open_pos = [x for x in open_pos if x["qty"] > 0]
            for sym in pending_entries:
                if len(open_pos) >= slots: break
                if any(x["sym"] == sym for x in open_pos): continue
                i = idx_of[sym].get(minute)
                if i is None: continue
                px = P[sym]["o"][i]
                if px <= 0: continue
                alloc = capital_per_trade  # ← fixed, no compound, no leverage
                qty = int(alloc // px)
                if qty <= 0: continue
                slp = p["long_sl"] if engine == "LONG" else p["short_sl"]
                sl_price = px * (1 - slp) if engine == "LONG" else px * (1 + slp)
                open_pos.append(dict(sym=sym, dir=engine, entry=px, qty=qty, qty0=qty,
                                     sl=sl_price, sl_pct=slp, tsl=None, tsl_on=False,
                                     partial_done=False, rsi_done=False, entry_i=i,
                                     entry_t=P[sym]["index"][i], candles=0))
            pending_entries = []
            for pos in open_pos:
                sym = pos["sym"]; i = idx_of[sym].get(minute)
                if i is None or pos["qty"] <= 0: continue
                if i == pos["entry_i"]:
                    _hit_sl(pos, P[sym], i, trades); continue
                pos["candles"] += 1
                dd = P[sym]
                if _hit_sl(pos, dd, i, trades): continue
                if p["pp_enabled"] and not pos["partial_done"]:
                    tgt = p["long_pt"] if pos["dir"] == "LONG" else p["short_pt"]
                    frac = p["long_pp_frac"] if pos["dir"] == "LONG" else p["short_pp_frac"]
                    if pos["dir"] == "LONG":
                        trig = pos["entry"] * (1 + tgt); hit = dd["h"][i] >= trig
                    else:
                        trig = pos["entry"] * (1 - tgt); hit = dd["l"][i] <= trig
                    if hit:
                        pos["partial_done"] = True
                        q = max(1, int(pos["qty"] * frac))
                        _close(trades, pos, trig, q, "PARTIAL_PROFIT", dd["index"][i])
                        if pos["qty"] <= 0: continue
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
                if pos["dir"] == "LONG" and p["ema50_exit"] and pos["candles"] >= p["min_hold"]:
                    if cpx < dd["e50"][i]:
                        pending_exits.append((pos, "EMA50_DYN")); continue
                if minute >= 915 or nxt is None:
                    _close(trades, pos, cpx, pos["qty"], "EOD", dd["index"][i])
            open_pos = [x for x in open_pos if x["qty"] > 0]
            if minute < 900 and len(open_pos) < slots:
                key = "long" if engine == "LONG" else "short"
                for sym in elig:
                    if any(x["sym"] == sym for x in open_pos): continue
                    i = idx_of[sym].get(minute)
                    if i is None: continue
                    if P[sym][key][i]:
                        pending_entries.append(sym)
        for pos in open_pos:
            if pos["qty"] > 0:
                st, en = sl_map[pos["sym"]]
                _close(trades, pos, P[pos["sym"]]["c"][en - 1], pos["qty"], "EOD_FORCE",
                       P[pos["sym"]]["index"][en - 1])
    return trades


# ── MAIN ANALYSIS ────────────────────────────────────────────────────
def main():
    print("Loading data...")
    dfs = load_all()
    P = precompute(dfs)
    gap, all_days = compute_gaps(dfs)
    syms = list(dfs.keys())
    bull, bear = regime_days_live(gap, all_days, syms,
                                  OPT["bull_gap"], OPT["bull_br"],
                                  OPT["bear_gap"], OPT["bear_br"])
    days = sorted([(d, "LONG") for d in bull] + [(d, "SHORT") for d in bear])
    print(f"Days: {len(all_days)} | Bull: {len(bull)} | Bear: {len(bear)}")

    # ── BASELINE OPTIMIZED RUN (5x margin, daily compound) ──
    print("Running BASELINE optimized backtest...")
    trades, final_cap = run(P, gap, days, OPT, capital0=100000.0)
    df = to_df(trades)
    print(f"  → {len(df)} trades, final cap = ₹{final_cap:,.0f}")

    # ── SECTION 1: FULL METRICS ──
    print("\n[1] Computing full metrics...")
    m_all = full_metrics(df, 100000.0, "OPTIMIZED (full)")
    m_long = full_metrics(df[df["dir"] == "LONG"], 100000.0, "LONG only")
    m_short = full_metrics(df[df["dir"] == "SHORT"], 100000.0, "SHORT only")
    per_year = per_year_table(df)

    # ── SECTION 2: WALK-FORWARD / YEAR SPLIT ──
    print("[2] Computing per-year & 2025/2026 split...")
    d_train = df[df["exit_date"] <= datetime.date(2024, 12, 31)]
    d_val = df[df["exit_date"] > datetime.date(2024, 12, 31)]
    d_2025 = df[df["year"] == 2025]
    d_2026 = df[df["year"] == 2026]
    m_train = full_metrics(d_train, 100000.0, "TRAIN 2022-24")
    m_val = full_metrics(d_val, 100000.0, "VAL 2025-26")
    m_2025 = full_metrics(d_2025, 100000.0, "2025 only")
    m_2026 = full_metrics(d_2026, 100000.0, "2026 only")

    # ── SECTION 3: TOP-5 REMOVED + EQUITY CURVE ──
    print("[3] Top-5 trades removal + month-end equity curve...")
    df_sorted_pnl = df.sort_values("net", ascending=False)
    top5 = df_sorted_pnl.head(5)
    df_no_top5 = df.drop(top5.index).sort_values("exit_t").reset_index(drop=True)
    # Recompute equity manually since we removed trades — capital doesn't compound the same way
    # so we compute PnL sum and metrics on the reduced trade set from ₹1L
    m_no_top5 = full_metrics(df_no_top5, 100000.0, "OPTIMIZED (top-5 removed)")
    equity_curve = month_end_equity(df, 100000.0)

    # ── SECTION 4: NO LEVERAGE, NO COMPOUNDING ──
    print("[4] No-leverage / no-compounding run (₹1L fixed per trade, 1x)...")
    trades_nc = run_no_compound(P, gap, days, OPT, capital_per_trade=100000.0)
    df_nc = to_df(trades_nc)
    m_nc = dict(
        label="NO-LEVERAGE / NO-COMPOUND (₹1L fixed, 1x)",
        n=len(df_nc),
        net=round(df_nc["net"].sum()),
        ret_pct=round(100 * df_nc["net"].sum() / 100000.0, 1),
        avg_pnl=round(df_nc["net"].mean(), 0) if not df_nc.empty else 0,
        wr=round(100 * (df_nc["net"] > 0).mean(), 1) if not df_nc.empty else 0,
    )
    if not df_nc.empty:
        wins = df_nc[df_nc["net"] > 0]["net"].sum()
        losses = -df_nc[df_nc["net"] <= 0]["net"].sum()
        m_nc["pf"] = round(wins / losses, 2) if losses > 0 else 99
        m_nc["avg_win"] = round(df_nc[df_nc["net"] > 0]["net"].mean(), 0)
        m_nc["avg_loss"] = round(df_nc[df_nc["net"] <= 0]["net"].mean(), 0)
    per_year_nc = per_year_table(df_nc)

    # ── SECTION 5: EMA50 ANALYSIS + TIME-EXIT ALT ──
    print("[5] EMA50 exit autopsy + 20-candle time-exit alt...")
    df_ema50 = df[df["reason"] == "EMA50_DYN"]
    ema50_stats = dict(
        n=len(df_ema50),
        net=round(df_ema50["net"].sum()),
        wr=round(100 * (df_ema50["net"] > 0).mean(), 1) if not df_ema50.empty else 0,
        avg=round(df_ema50["net"].mean(), 0) if not df_ema50.empty else 0,
        avg_win=round(df_ema50[df_ema50["net"] > 0]["net"].mean(), 0) if (df_ema50["net"] > 0).any() else 0,
        avg_loss=round(df_ema50[df_ema50["net"] <= 0]["net"].mean(), 0) if (df_ema50["net"] <= 0).any() else 0,
    )
    trades_te, _cap_te = run_with_time_exit(P, gap, days, OPT, time_exit_candles=20, capital0=100000.0)
    df_te = to_df(trades_te)
    m_te = full_metrics(df_te, 100000.0, "TIME_EXIT 20 candles (EMA50 replaced)")
    reason_split_te = (df_te.groupby("reason")["net"]
                       .agg(["count", "sum"]).round(0).to_dict("index")) if not df_te.empty else {}

    # ── SECTION 6: SHORT-ONLY PER YEAR ──
    print("[6] Short-only per-year breakdown...")
    short_per_year = per_year_direction(df, "SHORT")
    long_per_year = per_year_direction(df, "LONG")

    # Save trades for inspection
    df.to_csv("/app/research/robustness_optimized_trades.csv", index=False)
    df_nc.to_csv("/app/research/robustness_no_compound_trades.csv", index=False)
    df_te.to_csv("/app/research/robustness_time_exit_trades.csv", index=False)

    # ── RENDER MARKDOWN REPORT ──
    print("Rendering report...")
    lines = []
    W = lines.append
    W("# ROBUSTNESS ANALYSIS — Midcap50 Optimized Config")
    W(f"_Generated: {datetime.datetime.now().isoformat(timespec='seconds')}_")
    W("")
    W("**Setup:** 45 Midcap50 symbols, 5-min candles, Jan 2022 → Jul 2026.")
    W("**Config:** long_sl 1.2% · long_pt 3.5% · long_pp 0.75 · RSI-exit OFF · TSL 1.2x/0.5%; "
      "short_sl 0.5% · short_pt 2.0% · short_target_gap −2.5%; "
      "bull 0.7%/35% · bear −0.6%/40%; maxpos 1; 5× margin; ₹1L; daily compound.")
    W(f"**Base result:** ₹1L → ₹{final_cap:,.0f} on {len(df)} trades.")
    W("")

    # === 1. FULL METRICS ===
    W("## 1. Complete metrics table")
    W("")
    W("| Metric | Full | Long only | Short only |")
    W("|---|---:|---:|---:|")
    for k, lab in [("n", "Trades"), ("net", "Net PnL ₹"), ("ret_pct", "Return %"),
                   ("wr", "Win rate %"), ("avg_win", "Avg winner ₹"),
                   ("avg_loss", "Avg loser ₹"), ("pf", "Profit Factor"),
                   ("maxdd", "Max DD %"), ("sharpe", "Sharpe (annualized)"),
                   ("max_loss_streak", "Max losing streak"),
                   ("months_total", "Total months"),
                   ("months_negative", "Negative months"),
                   ("pct_months_negative", "% negative months")]:
        W(f"| {lab} | {m_all.get(k, '-')} | {m_long.get(k, '-')} | {m_short.get(k, '-')} |")
    W("")
    W("### Per-year breakdown (full portfolio, includes compounding)")
    W("")
    W("| Year | Trades | Win rate % | Return % | PF | Max DD % |")
    W("|---|---:|---:|---:|---:|---:|")
    for _, r in per_year.iterrows():
        W(f"| {r['year']} | {r['trades']} | {r['wr']} | {r['ret_pct']} | {r['pf']} | {r['maxdd']} |")
    W("")

    # === 2. WALK-FORWARD / YEAR SPLIT ===
    W("## 2. Walk-forward validation (out-of-sample stability)")
    W("")
    W("> **Note:** True walk-forward re-optimization (train per fold → apply to test) is a P1 backlog "
      "item explicitly deferred by the user. This section applies the SAME optimized config to each "
      "sub-period so you can see whether the parameter set is independently profitable in each fold.")
    W("")
    W("| Period | Trades | Ret % | PF | Win % | Max DD % | Sharpe |")
    W("|---|---:|---:|---:|---:|---:|---:|")
    for m in [m_train, m_val, m_2025, m_2026]:
        W(f"| {m['label']} | {m.get('n')} | {m.get('ret_pct')} | {m.get('pf')} | "
          f"{m.get('wr')} | {m.get('maxdd')} | {m.get('sharpe')} |")
    W("")
    W("**Read:** If both 2025 and 2026 are independently profitable with PF > 1.2, the edge is not a "
      "one-year artefact. If one year carries most of the val-period result, treat val PF cautiously.")
    W("")

    # === 3. TOP-5 REMOVED + EQUITY CURVE ===
    W("## 3. Top-5 trade dependency & equity curve smoothness")
    W("")
    W("### Top-5 winning trades (₹ PnL)")
    W("")
    W("| Rank | Symbol | Dir | Entry | Exit | Qty | Net ₹ | Reason | Entry time | Exit time |")
    W("|---:|---|---|---:|---:|---:|---:|---|---|---|")
    for i, (_, r) in enumerate(top5.iterrows(), 1):
        W(f"| {i} | {r['sym']} | {r['dir']} | {r['entry']:.2f} | {r['exit']:.2f} | "
          f"{r['qty']} | {int(r['net']):,} | {r['reason']} | {r['entry_t']} | {r['exit_t']} |")
    top5_pct = 100 * top5["net"].sum() / df["net"].sum()
    W(f"\n**Top-5 = {top5_pct:.1f}% of total net PnL** (₹{int(top5['net'].sum()):,} of ₹{int(df['net'].sum()):,})")
    W("")
    W("### Metrics with top-5 removed")
    W("")
    W("| Metric | Original | Top-5 removed |")
    W("|---|---:|---:|")
    for k, lab in [("n", "Trades"), ("net", "Net PnL ₹"), ("ret_pct", "Return %"),
                   ("wr", "Win rate %"), ("pf", "Profit Factor"),
                   ("maxdd", "Max DD %"), ("sharpe", "Sharpe")]:
        W(f"| {lab} | {m_all.get(k, '-')} | {m_no_top5.get(k, '-')} |")
    W("")
    W("### Month-end equity curve (compounded, ₹)")
    W("")
    W("| Month | Equity ₹ | Month return % |")
    W("|---|---:|---:|")
    for m, r in equity_curve.iterrows():
        W(f"| {m} | {int(r['equity']):,} | {r['month_ret_pct']:.1f} |")
    W("")

    # === 4. NO-LEVERAGE / NO-COMPOUNDING ===
    W("## 4. No-leverage, no-compounding (raw edge)")
    W("")
    W("> Fixed **₹1,00,000 per trade**, **1× margin (no leverage)**, **no daily compounding**. "
      "This strips out amplification and shows the raw per-trade edge.")
    W("")
    W("| Metric | Value |")
    W("|---|---:|")
    W(f"| Trades | {m_nc.get('n')} |")
    W(f"| Total net PnL ₹ | {m_nc.get('net'):,} |")
    W(f"| Total return % (on single ₹1L allocation, sum of all trades) | {m_nc.get('ret_pct')} |")
    W(f"| Avg PnL per trade ₹ | {int(m_nc.get('avg_pnl', 0)):,} |")
    W(f"| Avg winner ₹ | {int(m_nc.get('avg_win', 0)):,} |")
    W(f"| Avg loser ₹ | {int(m_nc.get('avg_loss', 0)):,} |")
    W(f"| Win rate % | {m_nc.get('wr')} |")
    W(f"| Profit Factor | {m_nc.get('pf', '-')} |")
    W("")
    W("### Per-year (no leverage, no compounding — capital reset ₹1L each trade)")
    W("")
    W("| Year | Trades | Win rate % | Net ₹ | PF |")
    W("|---|---:|---:|---:|---:|")
    if not df_nc.empty:
        for yr, g in df_nc.groupby("year"):
            wins = g[g["net"] > 0]["net"].sum(); losses = -g[g["net"] <= 0]["net"].sum()
            pf = round(wins / losses, 2) if losses > 0 else 99
            W(f"| {yr} | {len(g)} | {round(100 * (g['net'] > 0).mean(), 1)} | "
              f"{int(g['net'].sum()):,} | {pf} |")
    W("")

    # === 5. EMA50_DYN AUTOPSY + TIME-EXIT ALT ===
    W("## 5. EMA50_DYN exit — necessary evil or leak?")
    W("")
    W("### EMA50_DYN isolated stats (optimized run)")
    W("")
    W("| Metric | Value |")
    W("|---|---:|")
    W(f"| Trades exited via EMA50_DYN | {ema50_stats['n']} |")
    W(f"| Total PnL from EMA50 exits ₹ | {ema50_stats['net']:,} |")
    W(f"| Win rate % | {ema50_stats['wr']} |")
    W(f"| Avg PnL per EMA50 exit ₹ | {int(ema50_stats['avg']):,} |")
    W(f"| Avg winner in EMA50 subset ₹ | {int(ema50_stats['avg_win']):,} |")
    W(f"| Avg loser in EMA50 subset ₹ | {int(ema50_stats['avg_loss']):,} |")
    W("")
    W("### Replaced EMA50_DYN with 20-candle time exit (`TIME_EXIT`)")
    W("")
    W("| Metric | Original (EMA50) | Time-exit 20 candles |")
    W("|---|---:|---:|")
    for k, lab in [("n", "Trades"), ("ret_pct", "Return %"), ("pf", "Profit Factor"),
                   ("wr", "Win rate %"), ("maxdd", "Max DD %"),
                   ("sharpe", "Sharpe"), ("max_loss_streak", "Max losing streak")]:
        W(f"| {lab} | {m_all.get(k, '-')} | {m_te.get(k, '-')} |")
    W("")
    W("### Time-exit variant — exit reason breakdown")
    W("")
    W("| Reason | Count | Net ₹ |")
    W("|---|---:|---:|")
    for reason, r in reason_split_te.items():
        W(f"| {reason} | {int(r['count'])} | {int(r['sum']):,} |")
    W("")
    W("**Read:** If time-exit variant delivers ≥ EMA50 return with equal or better DD & Sharpe, "
      "EMA50_DYN is a leak. If EMA50 wins → it's a *necessary evil* (rides trend when active, cuts "
      "decayed signals into small losses that are still net-favourable overall).")
    W("")

    # === 6. SHORT-ONLY PER YEAR ===
    W("## 6. Short engine robustness — per-year breakdown")
    W("")
    W("### Short-only per year (5x margin, compounded)")
    W("")
    W("| Year | Trades | Win rate % | Net ₹ | Ret % | PF | Max DD % |")
    W("|---|---:|---:|---:|---:|---:|---:|")
    if not short_per_year.empty:
        for _, r in short_per_year.iterrows():
            W(f"| {r['year']} | {r['trades']} | {r['wr']} | {int(r['net']):,} | "
              f"{r['ret_pct']} | {r['pf']} | {r['maxdd']} |")
    else:
        W("| — no short trades — | | | | | | |")
    W("")
    W("### Long-only per year (for reference)")
    W("")
    W("| Year | Trades | Win rate % | Net ₹ | Ret % | PF | Max DD % |")
    W("|---|---:|---:|---:|---:|---:|---:|")
    if not long_per_year.empty:
        for _, r in long_per_year.iterrows():
            W(f"| {r['year']} | {r['trades']} | {r['wr']} | {int(r['net']):,} | "
              f"{r['ret_pct']} | {r['pf']} | {r['maxdd']} |")
    W("")
    W("**Read:** If shorts are net-negative in ≥2 of 5 years, the short engine is unproven and profits "
      "hinge on regime rarity. Consider running long-only during weak years.")
    W("")
    W("---")
    W("")
    W("### Files")
    W("- `research/robustness_optimized_trades.csv` — full baseline trades")
    W("- `research/robustness_no_compound_trades.csv` — no-leverage variant trades")
    W("- `research/robustness_time_exit_trades.csv` — 20-candle time-exit variant trades")

    with open(OUT, "w") as f:
        f.write("\n".join(lines))
    print(f"\n✅ Written: {OUT}")

    # Also emit a compact JSON summary
    summary = dict(
        base=m_all, long=m_long, short=m_short,
        train=m_train, val=m_val, y2025=m_2025, y2026=m_2026,
        no_top5=m_no_top5, no_compound=m_nc,
        ema50=ema50_stats, time_exit=m_te,
        top5_pct_of_total=round(top5_pct, 1),
    )
    with open("/app/research/robustness_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)


if __name__ == "__main__":
    main()
