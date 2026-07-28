"""Stage 5: JOINT grid — regime thresholds x maxpos x gap filters (interactions)."""
import sys, json, datetime
import pandas as pd
sys.path.insert(0, "/app/research")
from midcap50_optimizer import (load_all, precompute, compute_gaps, regime_days_live,
                                run, metrics, LIVE)

TRAIN_END = datetime.date(2024, 12, 31)
BEST = dict(LIVE, rsi_exit=False, min_hold=4, long_sl=0.012, long_pt=0.035,
            long_pp_frac=0.75, tsl_act=1.2, tsl_pct=0.005,
            short_sl=0.005, short_pt=0.02, short_target_gap=-0.02)

def sm(trades):
    tr = [t for t in trades if t["exit_t"].date() <= TRAIN_END]
    va = [t for t in trades if t["exit_t"].date() > TRAIN_END]
    return metrics(tr), metrics(va)

def main():
    dfs = load_all(); P = precompute(dfs)
    gap, all_days = compute_gaps(dfs); syms = list(dfs.keys())

    # ── JOINT: LONG engine — bull regime x maxpos x exclude-gap ──
    res = []
    for bg in [0.005, 0.007, 0.010]:
        for bb in [0.30, 0.35, 0.40]:
            bull, _ = regime_days_live(gap, all_days, syms, bg, bb, -0.006, 0.40)
            LD = sorted([(d, "LONG") for d in bull])
            for mp in [1, 2, 3]:
                for xg in [-0.004, -0.008, -0.012]:
                    p = dict(BEST, maxpos=mp, long_exclude_gap=xg)
                    mt, mv = sm(run(P, gap, LD, p)[0])
                    r = dict(bg=bg, bb=bb, mp=mp, xg=xg, ndays=len(bull),
                             t_ret=mt["ret"], t_pf=mt["pf"], t_dd=mt["maxdd"], t_n=mt["n"],
                             v_ret=mv["ret"], v_pf=mv["pf"], v_dd=mv["maxdd"], v_n=mv["n"])
                    res.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res).to_csv("/app/research/sweep_stage5_long_joint.csv", index=False)

    # ── JOINT: SHORT engine — bear regime x maxpos x target-gap ──
    res2 = []
    for rg in [-0.004, -0.006, -0.008]:
        for rb in [0.35, 0.40, 0.50]:
            _, bear = regime_days_live(gap, all_days, syms, 0.007, 0.35, rg, rb)
            SD = sorted([(d, "SHORT") for d in bear])
            for mp in [1, 2, 3]:
                for tg in [-0.015, -0.02, -0.025]:
                    p = dict(BEST, maxpos=mp, short_target_gap=tg)
                    mt, mv = sm(run(P, gap, SD, p)[0])
                    r = dict(rg=rg, rb=rb, mp=mp, tg=tg, ndays=len(bear),
                             t_ret=mt["ret"], t_pf=mt["pf"], t_dd=mt["maxdd"], t_n=mt["n"],
                             v_ret=mv["ret"], v_pf=mv["pf"], v_dd=mv["maxdd"], v_n=mv["n"])
                    res2.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res2).to_csv("/app/research/sweep_stage5_short_joint.csv", index=False)

if __name__ == "__main__":
    main()
