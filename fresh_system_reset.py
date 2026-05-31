#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Complete fresh system reset - removes ALL test/run data."""
import sys
import sqlite3
import json
import shutil
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

def reset_alcosoft_db():
    """Clear all data from alcosoft.db - keep schema only."""
    print("\n[1] Resetting alcosoft.db (production database)")
    print("=" * 60)

    db_path = Path('data/alcosoft.db')
    if not db_path.exists():
        print("alcosoft.db not found, skipping")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Clear all data but keep schema
    tables_to_clear = [
        'trades',
        'daily_stats',
        'agent_decision_log',
        'cognition_cycles',
        'cognition_daily_reflections',
        'cognition_hypotheses',
        'cognition_reviews',
    ]

    for table in tables_to_clear:
        try:
            cursor.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            if count > 0:
                cursor.execute(f'DELETE FROM {table}')
                # Reset autoincrement
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}'")
                print(f"  ✓ Cleared {table} ({count} rows)")
        except Exception as e:
            print(f"  ! Error clearing {table}: {e}")

    conn.commit()
    conn.close()
    print("  ✓ Database schema intact, data cleared")

def reset_reflection_db():
    """Clear all data from reflection.db"""
    print("\n[2] Resetting reflection.db (adaptive learning)")
    print("=" * 60)

    db_path = Path('data/reflection.db')
    if not db_path.exists():
        print("reflection.db not found, skipping")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    tables_to_clear = [
        'signal_performance',
        'symbol_behavior',
        'time_window_performance',
        'trade_records',
        'multiplier_history',
        'multiplier_change_log',
        'config_history',
    ]

    for table in tables_to_clear:
        try:
            cursor.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            if count > 0:
                cursor.execute(f'DELETE FROM {table}')
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}'")
                print(f"  ✓ Cleared {table} ({count} rows)")
        except Exception as e:
            print(f"  ! Error clearing {table}: {e}")

    conn.commit()
    conn.close()
    print("  ✓ Database schema intact, data cleared")

def reset_reflection_statistics_db():
    """Clear all data from reflection_statistics.db"""
    print("\n[3] Resetting reflection_statistics.db (market observations)")
    print("=" * 60)

    db_path = Path('data/reflection_statistics.db')
    if not db_path.exists():
        print("reflection_statistics.db not found, skipping")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    tables_to_clear = [
        'market_observations',
        'adaptive_config_history',
    ]

    for table in tables_to_clear:
        try:
            cursor.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            if count > 0:
                cursor.execute(f'DELETE FROM {table}')
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}'")
                print(f"  ✓ Cleared {table} ({count} rows)")
        except Exception as e:
            print(f"  ! Error clearing {table}: {e}")

    conn.commit()
    conn.close()
    print("  ✓ Database schema intact, data cleared")

def clear_json_data_files():
    """Clear all JSON data files (positions, briefing, reflections, etc.)"""
    print("\n[4] Clearing JSON data files")
    print("=" * 60)

    files_to_clear = [
        'data/positions.json',
        'data/session_briefing.json',
        'data/live_capital.json',
    ]

    for file_path_str in files_to_clear:
        file_path = Path(file_path_str)
        if file_path.exists():
            file_path.unlink()
            print(f"  ✓ Removed {file_path_str}")

    # Clear reflection directories
    for dir_name in ['data/reflections', 'data/reflection_snapshots']:
        dir_path = Path(dir_name)
        if dir_path.exists():
            shutil.rmtree(dir_path)
            dir_path.mkdir()
            print(f"  ✓ Cleared {dir_name}/ (recreated empty)")

def verify_reset():
    """Verify the reset was successful."""
    print("\n[5] Verification")
    print("=" * 60)

    db_path = Path('data/alcosoft.db')
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM trades")
        trades_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM daily_stats")
        daily_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM cognition_cycles")
        cog_count = cursor.fetchone()[0]

        conn.close()

        print(f"  alcosoft.db:")
        print(f"    - trades: {trades_count} rows")
        print(f"    - daily_stats: {daily_count} rows")
        print(f"    - cognition_cycles: {cog_count} rows")

    print(f"\n  JSON data files:")
    if not Path('data/positions.json').exists():
        print(f"    - positions.json: removed")
    if not Path('data/session_briefing.json').exists():
        print(f"    - session_briefing.json: removed")

def main():
    print("\n" + "=" * 60)
    print("ALCOSOFT FRESH SYSTEM RESET")
    print("Clearing ALL test/run data - schema preserved")
    print("=" * 60)

    reset_alcosoft_db()
    reset_reflection_db()
    reset_reflection_statistics_db()
    clear_json_data_files()
    verify_reset()

    print("\n" + "=" * 60)
    print("✅ SYSTEM RESET COMPLETE - FRESH START READY")
    print("=" * 60)
    print("\nWhen you run the app next:")
    print("  - All databases are empty")
    print("  - No historical trades or positions")
    print("  - No adaptive learning data")
    print("  - Config settings preserved (trading_settings.json)")
    print("  - Ready for production trading\n")

if __name__ == "__main__":
    main()
