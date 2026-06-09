import sqlite3
import os

DB_PATH = '/home/ubuntu/alcosoft/data/alcosoft.db'
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE daily_stats SET capital_start = NULL WHERE date >= '2026-06-10'")
conn.commit()
conn.close()
print("Successfully nullified today's capital_start.")
