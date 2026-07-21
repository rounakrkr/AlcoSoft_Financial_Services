"""Stage 2: fine params for chosen exit structure. Stage 3: regime + sizing."""
import sys, json, time, datetime
import pandas as pd
sys.path.insert(0, "/app/research")
from midcap50_optimizer import (load_all, precompute, compute_gaps, regime_days_live,
                                run, metrics, LIVE)

TRAIN_END = datetime.date(2024, 12, 31)

def sm(trades):
    tr = [t for t in trades if t["exit_t"].date() <= TRAIN_END]
    va = [t for t in trades if t["exit_t"].date() > TRAIN_END]
    return metrics(tr), metrics(va)

def row(engine, g, mt, mv):
    return dict(engine=engine, **{k: v for k, v in g.items()},
                train_ret=mt["ret"], train_pf=mt["pf"], train_n=mt["n"], train_dd=mt["maxdd"],
                val_ret=mv["ret"], val_pf=mv["pf"], val_n=mv["n"], val_dd=mv["maxdd"])

BASE_LONG = dict(LIVE, rsi_exit=False, ema50_exit=True, min_hold=4)
BASE_SHORT = dict(LIVE, short_sl=0.005)

def main():
    dfs = load_all(); P = precompute(dfs)
    gap, all_days = compute_gaps(dfs); syms = list(dfs.keys())
    bull, bear = regime_days_live(gap, all_days, syms, LIVE["bull_gap"], LIVE["bull_br"],
                                  LIVE["bear_gap"], LIVE["bear_br"])
    LD = sorted([(d, "LONG") for d in bull]); SD = sorted([(d, "SHORT") for d in bear])

    res = []
    # STAGE 2A: LONG SL / PT / partial fraction
    for sl in [0.008, 0.01, 0.012]:
        for pt in [0.015, 0.02, 0.025, 0.035]:
            for frac in [0.25, 0.5, 0.75]:
                g = dict(long_sl=sl, long_pt=pt, long_pp_frac=frac)
                p = dict(BASE_LONG); p.update(g)
                mt, mv = sm(run(P, gap, LD, p)[0])
                r = row("LONG_SLPT", g, mt, mv); res.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res).to_csv("/app/research/sweep_stage2a.csv", index=False)

    res2 = []
    # STAGE 2B: LONG TSL variants
    for act in [1.2, 1.4, 1.8, 99]:  # 99 = TSL effectively off
        for tp in [0.005, 0.008, 0.012]:
            g = dict(tsl_act=act, tsl_pct=tp)
            p = dict(BASE_LONG); p.update(g)
            mt, mv = sm(run(P, gap, LD, p)[0])
            r = row("LONG_TSL", g, mt, mv); res2.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res2).to_csv("/app/research/sweep_stage2b.csv", index=False)

    res3 = []
    # STAGE 2C: SHORT PT + gap filter
    for pt in [0.015, 0.02, 0.025, 0.035]:
        for tg in [-0.008, -0.012, -0.015, -0.02]:
            g = dict(short_pt=pt, short_target_gap=tg)
            p = dict(BASE_SHORT); p.update(g)
            mt, mv = sm(run(P, gap, SD, p)[0])
            r = row("SHORT_PT", g, mt, mv); res3.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res3).to_csv("/app/research/sweep_stage2c.csv", index=False)

    res4 = []
    # STAGE 3: regime thresholds (both engines together) + maxpos
    for bg, bb in [(0.005, 0.30), (0.005, 0.35), (0.007, 0.30), (0.007, 0.35), (0.007, 0.40), (0.010, 0.35), (0.010, 0.40)]:
        for rg, rb in [(-0.006, 0.40)]:
            bl, br_ = regime_days_live(gap, all_days, syms, bg, bb, rg, rb)
            days = sorted([(d, "LONG") for d in bl] + [(d, "SHORT") for d in br_])
            p = dict(BASE_LONG)
            mt, mv = sm(run(P, gap, days, p)[0])
            g = dict(bull_gap=bg, bull_br=bb, nbull=len(bl), nbear=len(br_))
            r = row("REGIME_BULL", g, mt, mv); res4.append(r); print(json.dumps(r), flush=True)
    for rg, rb in [(-0.004, 0.40), (-0.006, 0.35), (-0.006, 0.40), (-0.006, 0.50), (-0.008, 0.40)]:
        bl, br_ = regime_days_live(gap, all_days, syms, LIVE["bull_gap"], LIVE["bull_br"], rg, rb)
        days = sorted([(d, "SHORT") for d in br_])
        p = dict(BASE_SHORT)
        mt, mv = sm(run(P, gap, days, p)[0])
        g = dict(bear_gap=rg, bear_br=rb, nbear=len(br_))
        r = row("REGIME_BEAR", g, mt, mv); res4.append(r); print(json.dumps(r), flush=True)
    for mp in [1, 2, 3]:
        p = dict(BASE_LONG, maxpos=mp)
        mt, mv = sm(run(P, gap, LD, p)[0])
        r = row("MAXPOS_LONG", dict(maxpos=mp), mt, mv); res4.append(r); print(json.dumps(r), flush=True)
    pd.DataFrame(res4).to_csv("/app/research/sweep_stage3.csv", index=False)

if __name__ == "__main__":
    main()
