#!/usr/bin/env python3
"""Audit current database state."""
import sqlite3
from pathlib import Path

# Audit alcosoft.db
db_path = Path('data/alcosoft.db')
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    print('[alcosoft.db]')
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        print(f'  {table}: {count} rows')

    # Distribution of paper vs live
    print('\ntrading_mode distribution (trades):')
    cursor.execute('SELECT trading_mode, COUNT(*) FROM trades GROUP BY trading_mode')
    for mode, count in cursor.fetchall():
        print(f'  {mode}: {count}')

    # Check if positions are from test data
    print('\nRecent open positions:')
    cursor.execute('SELECT symbol, trading_mode, entry_time FROM trades WHERE status="OPEN" ORDER BY id DESC LIMIT 5')
    for row in cursor.fetchall():
        print(f'  {row[0]} ({row[1]}) @ {row[2]}')

    conn.close()

# Audit reflection.db
print('\n[reflection.db]')
db_path = Path('data/reflection.db')
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        print(f'  {table}: {count} rows')

    conn.close()

# Audit reflection_statistics.db
print('\n[reflection_statistics.db]')
db_path = Path('data/reflection_statistics.db')
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        print(f'  {table}: {count} rows')

    conn.close()

print('\nDone.')
