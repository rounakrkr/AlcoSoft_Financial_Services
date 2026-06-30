import sys
import pandas as pd
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_cache import load_cache

df = load_cache()["APOLLOHOSP"]
df = df[df.index.date == pd.to_datetime("2026-03-20").date()]
for i, (ts, row) in enumerate(df.iterrows()):
    print(f"idx={i}, ts={ts}, rsi_14={row.get('rsi_14', 'N/A')}")
