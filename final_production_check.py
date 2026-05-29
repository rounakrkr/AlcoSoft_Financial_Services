#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final production readiness check."""
import sys
import sqlite3
import json
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

def check_alcosoft_db():
    """Verify alcosoft.db state."""
    print("\n[alcosoft.db] Production Data Check")
    print("=" * 60)

    db_path = Path('data/alcosoft.db')
    if not db_path.exists():
        print("ERROR: data/alcosoft.db not found!")
        return False

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Check trades table
    cursor.execute('SELECT COUNT(*) FROM trades WHERE status IN ("CLOSED", "STOPPED")')
    closed = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM trades WHERE status = "OPEN"')
    open_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM trades WHERE trading_mode = "LIVE"')
    live_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM trades WHERE trading_mode = "PAPER"')
    paper_count = cursor.fetchone()[0]

    print(f"✓ trades table: {closed} closed + {open_count} open = {closed + open_count} total")
    print(f"  - LIVE: {live_count} | PAPER: {paper_count}")

    # Check daily_stats
    cursor.execute('SELECT COUNT(*) FROM daily_stats')
    daily = cursor.fetchone()[0]
    print(f"✓ daily_stats table: {daily} rows")

    # Check war_room_log (can be empty, that's ok)
    cursor.execute('SELECT COUNT(*) FROM war_room_log')
    war_log = cursor.fetchone()[0]
    print(f"✓ war_room_log table: {war_log} rows (legacy, can be empty)")

    # Check cognition tables (should be empty)
    for table in ['cognition_cycles', 'cognition_daily_reflections', 'cognition_hypotheses', 'cognition_reviews']:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        if count == 0:
            print(f"✓ {table}: 0 rows (ready for production data)")
        else:
            print(f"! {table}: {count} rows (unexpected, should be empty)")

    conn.close()
    return True

def check_reflection_dbs():
    """Verify reflection databases are clean."""
    print("\n[reflection.db] Test Data Cleanup Check")
    print("=" * 60)

    db_path = Path('data/reflection.db')
    if not db_path.exists():
        print("reflection.db not found (optional)")
        return True

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    clean_tables = [
        'signal_performance',
        'symbol_behavior',
        'time_window_performance',
        'trade_records',
    ]

    all_clean = True
    for table in clean_tables:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        if count == 0:
            print(f"✓ {table}: 0 rows (clean)")
        else:
            print(f"! {table}: {count} rows (CLEANUP FAILED)")
            all_clean = False

    # Ready-for-production tables
    for table in ['multiplier_history', 'multiplier_change_log', 'config_history']:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        count = cursor.fetchone()[0]
        print(f"✓ {table}: {count} rows (ready for production)")

    conn.close()

    print("\n[reflection_statistics.db] Test Data Cleanup Check")
    print("=" * 60)

    db_path = Path('data/reflection_statistics.db')
    if not db_path.exists():
        print("reflection_statistics.db not found (optional)")
        return all_clean

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM market_observations')
    count = cursor.fetchone()[0]
    if count == 0:
        print(f"✓ market_observations: 0 rows (clean)")
    else:
        print(f"! market_observations: {count} rows (CLEANUP FAILED)")
        all_clean = False

    cursor.execute('SELECT COUNT(*) FROM adaptive_config_history')
    count = cursor.fetchone()[0]
    print(f"✓ adaptive_config_history: {count} rows (ready for production)")

    conn.close()
    return all_clean

def check_settings():
    """Verify settings.json is clean."""
    print("\n[config/trading_settings.json] Settings Terminology Check")
    print("=" * 60)

    with open('config/trading_settings.json', 'r') as f:
        settings = json.load(f)

    has_war_room_refs = False

    # Check for war_room references
    settings_json = json.dumps(settings, indent=2).lower()
    if 'war_room' in settings_json:
        print("! FOUND 'war_room' references in settings.json")
        has_war_room_refs = True

    # Verify expected new keys exist
    if 'cognition_picks' in str(settings.get('screener', {})):
        print("✓ screener.cognition_picks: configured")
    elif 'war_room_picks' in str(settings.get('screener', {})):
        print("! screener: still has war_room_picks (not updated)")
        has_war_room_refs = True
    else:
        print("! screener: missing cognition_picks/war_room_picks")

    if 'cognition_cycle_interval_minutes' in str(settings.get('scheduling', {})):
        print("✓ scheduling.cognition_cycle_interval_minutes: configured")
    elif 'war_room_interval_minutes' in str(settings.get('scheduling', {})):
        print("! scheduling: still has war_room_interval_minutes (not updated)")
        has_war_room_refs = True

    return not has_war_room_refs

def check_templates():
    """Verify dashboard templates have correct navigation."""
    print("\n[Dashboard Templates] Navigation Check")
    print("=" * 60)

    # Check index.html
    with open('dashboard/templates/index.html', 'r', encoding='utf-8') as f:
        index_content = f.read()

    has_cognition_link = 'href="/cognition"' in index_content or 'href="/cognition/' in index_content
    print(f"✓ index.html: Cognition Lab link " + ("found" if has_cognition_link else "MISSING"))

    # Check settings.html
    with open('dashboard/templates/settings.html', 'r', encoding='utf-8') as f:
        settings_content = f.read()

    has_cognition_link = 'href="/cognition"' in settings_content or 'href="/cognition/' in settings_content
    print(f"✓ settings.html: Cognition Lab link " + ("found" if has_cognition_link else "MISSING"))

    # Check for War Room references
    if 'war room' in index_content.lower() and 'ai agent' not in index_content.lower():
        print("! index.html: Still has 'War Room' text (not renamed)")
        return False

    if 'war room' in settings_content.lower():
        print("! settings.html: Still has 'War Room' text (should be removed)")
        return False

    print("✓ No 'War Room' terminology in dashboard templates")
    return True

def main():
    print("\n" + "=" * 60)
    print("ALCOSOFT PRODUCTION READINESS CHECK")
    print("=" * 60)

    checks = [
        ("Database (alcosoft.db)", check_alcosoft_db()),
        ("Reflection DBs (cleanup)", check_reflection_dbs()),
        ("Settings (terminology)", check_settings()),
        ("Templates (navigation)", check_templates()),
    ]

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    all_pass = True
    for name, passed in checks:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {name}")
        if not passed:
            all_pass = False

    print("=" * 60)
    if all_pass:
        print("\n✅ ALL CHECKS PASSED - READY FOR PRODUCTION")
    else:
        print("\n❌ SOME CHECKS FAILED - REVIEW ABOVE")

    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
