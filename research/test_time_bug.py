import pandas as pd
from dataclasses import dataclass
from typing import List

@dataclass
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    direction: str
    exit_reason: str
    pnl_net: float = 0.0

trades = []
# Simulate
ts = pd.Timestamp("2026-03-20 10:40:00+05:30")
entry_time = pd.Timestamp("2026-03-20 10:40:00+05:30")

print(f"ts: {ts}")
print(f"entry_time: {entry_time}")
print(f"ts > entry_time: {ts > entry_time}")

# We read the tearsheet directly to see what the python script parsed
with open(r'c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\dual_engine_tearsheet.txt', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    for line in lines:
        if "APOLLOHOSP" in line and "RSI_OVERBOUGHT" in line:
            print(line.strip())
