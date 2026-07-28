"""Stage 4: combined best-param candidates vs LIVE baseline, full portfolio (both engines)."""
import sys, json, datetime
import pandas as pd
sys.path.insert(0, "/app/research")
from midcap50_optimizer import (load_all, precompute, compute_gaps, regime_days_live,
                                run, metrics, LIVE)

TRAIN_END = datetime.date(2024, 12, 31)

def sm(trades):
    tr = [t for t in trades if t["exit_t"].date() <= TRAIN_END]
    va = [t for t in trades if t["exit_t"].date() > TRAIN_END]
    return metrics(tr), metrics(va), metrics(trades)

CANDIDATES = {
    "LIVE_BASELINE": dict(LIVE),
    # A: full best combo
    "A_BEST": dict(LIVE, rsi_exit=False, min_hold=4, long_sl=0.012, long_pt=0.035,
                   long_pp_frac=0.75, tsl_act=1.2, tsl_pct=0.005,
                   short_sl=0.005, short_pt=0.02, short_target_gap=-0.02),
    # B: conservative (keep PT 2.5, frac 0.5)
    "B_MODERATE": dict(LIVE, rsi_exit=False, min_hold=4, long_sl=0.012, long_pt=0.025,
                       long_pp_frac=0.5, tsl_act=1.2, tsl_pct=0.005,
                       short_sl=0.005, short_pt=0.025, short_target_gap=-0.02),
    # C: A but keep RSI exit 78 (check interaction w/ tight TSL)
    "C_A_RSI78": dict(LIVE, rsi_exit=True, long_rsi=78.0, min_hold=4, long_sl=0.012,
                      long_pt=0.035, long_pp_frac=0.75, tsl_act=1.2, tsl_pct=0.005,
                      short_sl=0.005, short_pt=0.02, short_target_gap=-0.02),
    # D: A with maxpos 3 (dd reduction check)
    "D_A_MP3": dict(LIVE, rsi_exit=False, min_hold=4, long_sl=0.012, long_pt=0.035,
                    long_pp_frac=0.75, tsl_act=1.2, tsl_pct=0.005,
                    short_sl=0.005, short_pt=0.02, short_target_gap=-0.02, maxpos=3),
    # E: A with stricter bull regime 1.0%/40% (PF/dd improvement check)
    "E_A_STRICT_REGIME": dict(LIVE, rsi_exit=False, min_hold=4, long_sl=0.012, long_pt=0.035,
                              long_pp_frac=0.75, tsl_act=1.2, tsl_pct=0.005,
                              short_sl=0.005, short_pt=0.02, short_target_gap=-0.02,
                              bull_gap=0.010, bull_br=0.40),
}

def main():
    dfs = load_all(); P = precompute(dfs)
    gap, all_days = compute_gaps(dfs); syms = list(dfs.keys())
    rows = []
    for name, p in CANDIDATES.items():
        bull, bear = regime_days_live(gap, all_days, syms, p["bull_gap"], p["bull_br"],
                                      p["bear_gap"], p["bear_br"])
        days = sorted([(d, "LONG") for d in bull] + [(d, "SHORT") for d in bear])
        trades, cap = run(P, gap, days, p)
        mt, mv, ma = sm(trades)
        r = dict(name=name, final_cap=round(cap), total_ret=ma["ret"], total_pf=ma["pf"],
                 total_dd=ma["maxdd"], n=ma["n"], wr=ma["wr"],
                 train_ret=mt["ret"], train_pf=mt["pf"], train_dd=mt["maxdd"],
                 val_ret=mv["ret"], val_pf=mv["pf"], val_dd=mv["maxdd"])
        rows.append(r); print(json.dumps(r), flush=True)
        if name == "A_BEST":
            pd.DataFrame(trades).to_csv("/app/research/mc50_best_trades.csv", index=False)
    pd.DataFrame(rows).to_csv("/app/research/sweep_stage4_final.csv", index=False)

if __name__ == "__main__":
    main()
