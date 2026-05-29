#!/usr/bin/env python3
import sqlite3
from pathlib import Path
import sys

# Force UTF-8 output
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

db_path = Path("data/alcosoft.db")
if not db_path.exists():
    print("Database not found")
    exit(1)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Get all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = [row[0] for row in cursor.fetchall()]

print("=" * 60)
print("DATABASE TABLES & ROW COUNTS")
print("=" * 60)

for table in tables:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    count = cursor.fetchone()[0]
    print(f"\nTable: {table} - {count} rows")

    # Show schema
    cursor.execute(f"PRAGMA table_info({table})")
    columns = cursor.fetchall()
    for col in columns[:5]:  # First 5 columns
        print(f"  - {col[1]} ({col[2]})")
    if len(columns) > 5:
        print(f"  ... +{len(columns)-5} more columns")

conn.close()

# Size
import os
size_mb = os.path.getsize(str(db_path)) / 1024 / 1024
print(f"\nTotal DB size: {size_mb:.2f} MB")
