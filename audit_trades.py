#!/usr/bin/env python3
import sqlite3
from pathlib import Path
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

db_path = Path("data/alcosoft.db")
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Check trades table
cursor.execute("""
    SELECT date, COUNT(*) as count,
           SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) as open_count,
           SUM(CASE WHEN trading_mode='LIVE' THEN 1 ELSE 0 END) as live_count,
           SUM(CASE WHEN trading_mode='PAPER' THEN 1 ELSE 0 END) as paper_count
    FROM trades
    GROUP BY date
    ORDER BY date DESC
    LIMIT 10
""")

print("=" * 80)
print("TRADES BREAKDOWN BY DATE")
print("=" * 80)
for row in cursor.fetchall():
    date, count, open_c, live_c, paper_c = row
    print(f"  {date}: {count} total | {open_c} OPEN | {live_c} LIVE | {paper_c} PAPER")

# Check for stale open positions
cursor.execute("""
    SELECT symbol, entry_price, quantity, entry_time, trading_mode
    FROM trades
    WHERE status='OPEN'
    ORDER BY date DESC
    LIMIT 20
""")

print("\n" + "=" * 80)
print("STALE OPEN POSITIONS (POTENTIAL BUG)")
print("=" * 80)
open_trades = cursor.fetchall()
if open_trades:
    for row in open_trades:
        print(f"  {row[0]} x{row[2]} @ Rs{row[1]} | Entry: {row[3]} | Mode: {row[4]}")
else:
    print("  None (good)")

# Daily stats
cursor.execute("SELECT * FROM daily_stats ORDER BY date DESC LIMIT 1")
stats = cursor.fetchone()
if stats:
    print(f"\n" + "=" * 80)
    print(f"LATEST DAILY STATS")
    print(f"=" * 80)
    print(f"  Date: {stats[1]}")
    print(f"  Trades: {stats[2]} | Winners: {stats[3]} | Losers: {stats[4]}")
    print(f"  Gross P&L: Rs{stats[5]:.2f}")
    print(f"  Capital Start: {stats[6]} | Capital End: {stats[7]}")

conn.close()
