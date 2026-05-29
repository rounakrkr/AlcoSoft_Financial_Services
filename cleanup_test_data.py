#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clear test data from reflection databases."""
import sys
import sqlite3
from pathlib import Path

# Fix encoding on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

def cleanup_reflection_db():
    """Clear test data from reflection.db"""
    db_path = Path('data/reflection.db')
    if not db_path.exists():
        print("reflection.db not found, skipping")
        return

    conn = sqlite3.connect(str(db_path))

    tables_to_clear = [
        'signal_performance',
        'symbol_behavior',
        'time_window_performance',
        'trade_records',
    ]

    for table in tables_to_clear:
        try:
            cursor = conn.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            if count > 0:
                conn.execute(f'DELETE FROM {table}')
                print(f"✓ Cleared {table} ({count} rows)")
        except Exception as e:
            print(f"✗ Error clearing {table}: {e}")

    conn.commit()
    conn.close()

def cleanup_reflection_statistics_db():
    """Clear test data from reflection_statistics.db"""
    db_path = Path('data/reflection_statistics.db')
    if not db_path.exists():
        print("reflection_statistics.db not found, skipping")
        return

    conn = sqlite3.connect(str(db_path))

    tables_to_clear = [
        'market_observations',
    ]

    for table in tables_to_clear:
        try:
            cursor = conn.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            if count > 0:
                conn.execute(f'DELETE FROM {table}')
                print(f"✓ Cleared {table} ({count} rows)")
        except Exception as e:
            print(f"✗ Error clearing {table}: {e}")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    print("Cleaning up test data...\n")
    cleanup_reflection_db()
    cleanup_reflection_statistics_db()
    print("\n✅ Test data cleanup complete")
