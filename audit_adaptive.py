#!/usr/bin/env python3
"""Audit all adaptive learning and reflection databases."""
import sqlite3
from pathlib import Path

dbs = [
    "data/reflection.db",
    "data/reflection_statistics.db",
]

for db_file in dbs:
    db_path = Path(db_file)
    if not db_path.exists():
        print(f"[SKIP] {db_file} — not found")
        continue

    print(f"\n{'='*70}")
    print(f"[DB] {db_file}")
    print('='*70)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"\n  {table}: {count} rows")

        # Show first few rows
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"    Columns: {', '.join(columns[:5])}" + (f"... +{len(columns)-5}" if len(columns) > 5 else ""))

    conn.close()

print("\n" + "="*70)
print("REFLECTION FILES")
print("="*70)

ref_dir = Path("data/reflections")
if ref_dir.exists():
    files = list(ref_dir.glob("*.json"))
    print(f"\nReflection JSONs: {len(files)} files")
    for f in sorted(files)[-5:]:
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name} ({size_kb:.1f} KB)")

snap_dir = Path("data/reflection_snapshots")
if snap_dir.exists():
    files = list(snap_dir.glob("*.json"))
    print(f"\nReflection Snapshots: {len(files)} files")
    for f in sorted(files)[-5:]:
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name} ({size_kb:.1f} KB)")
