import pandas as pd
from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from core.strategy import _build_indicators

def test_vwap():
    # Create 3 days of dummy 1-minute data
    dates = []
    base_date = datetime(2026, 6, 8, 9, 15)
    for day in range(3):
        for minute in range(30):
            dates.append(base_date + timedelta(days=day, minutes=minute))
            
    df = pd.DataFrame({
        "open": [100.0] * 90,
        "high": [105.0] * 90,
        "low": [95.0] * 90,
        "close": [102.0] * 90,
        "volume": [1000] * 90
    }, index=pd.DatetimeIndex(dates))
    
    try:
        out_df = _build_indicators(df)
        print("VWAP calculated successfully!")
        print("First day VWAP end:", out_df["vwap"].iloc[29])
        print("Second day VWAP start:", out_df["vwap"].iloc[30])
        print("If grouped by day, they should reset and be exactly the same for constant data.")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    test_vwap()
