import re

log_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\acbe5f97-2544-4468-b175-d002090fd989\.system_generated\tasks\task-1402.log"

results = []
with open(log_path, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        # Match result lines: label | WR=X% | Net=Y | Ret=Z% | T=N
        m = re.search(r'(RSI\([01]\)[^|]+)\|\s*WR=\s*([\d.]+)%\s*\|\s*Net=\s*([+\-\d,]+)\s*\|\s*Ret=\s*([+\-\d.]+)%\s*\|\s*T=\s*(\d+)', line)
        if m:
            label = m.group(1).strip()
            wr    = float(m.group(2))
            ret   = float(m.group(4))
            t     = int(m.group(5))
            results.append({"label": label, "wr": wr, "ret": ret, "t": t})

print(f"Total configs parsed: {len(results)}\n")

print("="*110)
print("TOP 20 by RETURN")
print("="*110)
top_ret = sorted(results, key=lambda x: x["ret"], reverse=True)[:20]
for i, rx in enumerate(top_ret, 1):
    best = " <<<< KING" if i == 1 else (" ***" if i <= 5 else "")
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{best}")

print()
print("="*110)
print("TOP 20 by WIN RATE (positive return only)")
print("="*110)
top_wr = sorted([x for x in results if x["ret"] > 0], key=lambda x: x["wr"], reverse=True)[:20]
for i, rx in enumerate(top_wr, 1):
    best = " <<<< KING" if i == 1 else (" ***" if i <= 5 else "")
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}{best}")

print()
print("="*110)
print("SWEET SPOT: WR >= 55% AND Return >= 25% (best of both worlds)")
print("="*110)
sweet = sorted([x for x in results if x["wr"] >= 55 and x["ret"] >= 25],
               key=lambda x: x["ret"], reverse=True)
for i, rx in enumerate(sweet, 1):
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")
if not sweet:
    print("  (none found with WR>=55% AND Ret>=25%)")

print()
print("="*110)
print("HIGH WR ZONE: WR >= 57% (all positive return configs)")
print("="*110)
highwr = sorted([x for x in results if x["wr"] >= 57 and x["ret"] > 0],
                key=lambda x: x["ret"], reverse=True)
for i, rx in enumerate(highwr, 1):
    print(f"  #{i:2d} | {rx['label']:65s} | WR={rx['wr']:5.1f}% | Ret={rx['ret']:+6.1f}% | T={rx['t']:4d}")
if not highwr:
    print("  (none found with WR>=57%)")
