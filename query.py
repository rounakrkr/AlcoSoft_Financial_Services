import sqlite3
conn = sqlite3.connect('/home/ubuntu/alcosoft/data/alcosoft.db')
print(conn.execute("SELECT date, capital_start, capital_end FROM daily_stats ORDER BY date DESC LIMIT 5").fetchall())
