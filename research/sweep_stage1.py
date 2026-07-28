"""Staged parameter sweep — LONG and SHORT engines independently, train/valid split."""
import sys, json, time, itertools, datetime
import pandas as pd
sys.path.insert(0, "/app/research")
from midcap50_optimizer import (load_all, precompute, compute_gaps, regime_days_live,
                                run, metrics, LIVE)

TRAIN_END = datetime.date(2024, 12, 31)

def split_metrics(trades):
    tr = [t for t in trades if t["exit_t"].date() <= TRAIN_END]
    va = [t for t in trades if t["exit_t"].date() > TRAIN_END]
    return metrics(tr), metrics(va)

def main():
    dfs = load_all()
    P = precompute(dfs)
    gap, all_days = compute_gaps(dfs)
    syms = list(dfs.keys())
    bull, bear = regime_days_live(gap, all_days, syms, LIVE["bull_gap"], LIVE["bull_br"],
                                  LIVE["bear_gap"], LIVE["bear_br"])
    print(f"bull days={len(bull)} bear days={len(bear)}")
    long_days = sorted([(d, "LONG") for d in bull])
    short_days = sorted([(d, "SHORT") for d in bear])

    results = []
    # ── STAGE 1: LONG exit structure ──
    grid = []
    for ema50_exit, min_hold in [(True, 4), (True, 12), (True, 20), (False, 0)]:
        for long_rsi in [72.0, 78.0, 999]:
            grid.append(dict(ema50_exit=ema50_exit, min_hold=min_hold,
                             rsi_exit=long_rsi < 999, long_rsi=long_rsi))
    for g in grid:
        p = dict(LIVE); p.update(g)
        t0 = time.time()
        trades, _ = run(P, gap, long_days, p)
        mt, mv = split_metrics(trades)
        row = dict(engine="LONG", **g, train_ret=mt["ret"], train_pf=mt["pf"], train_n=mt["n"],
                   train_dd=mt["maxdd"], val_ret=mv["ret"], val_pf=mv["pf"], val_n=mv["n"], val_dd=mv["maxdd"])
        results.append(row)
        print(json.dumps(row), f"({time.time()-t0:.0f}s)", flush=True)

    pd.DataFrame(results).to_csv("/app/research/sweep_stage1_long.csv", index=False)

    # ── STAGE 1b: SHORT exit structure ──
    results2 = []
    for short_rsi in [15.0, 17.0, 20.0, 999]:
        for short_sl in [0.005, 0.008, 0.010]:
            g = dict(rsi_exit=short_rsi < 999, short_rsi=short_rsi, short_sl=short_sl)
            p = dict(LIVE); p.update(g)
            trades, _ = run(P, gap, short_days, p)
            mt, mv = split_metrics(trades)
            row = dict(engine="SHORT", **g, train_ret=mt["ret"], train_pf=mt["pf"], train_n=mt["n"],
                       train_dd=mt["maxdd"], val_ret=mv["ret"], val_pf=mv["pf"], val_n=mv["n"], val_dd=mv["maxdd"])
            results2.append(row)
            print(json.dumps(row), flush=True)
    pd.DataFrame(results2).to_csv("/app/research/sweep_stage1_short.csv", index=False)

if __name__ == "__main__":
    main()
