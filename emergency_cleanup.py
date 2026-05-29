#!/usr/bin/env python3
"""
EMERGENCY CLEANUP: Close stale positions and fix database inconsistencies.
Run once to clean up before going live.
"""
import sqlite3
from pathlib import Path
from datetime import datetime
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = Path("data/alcosoft.db")

def close_stale_positions():
    """Close any OPEN positions that should have been closed."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Get all open positions
    cursor.execute("""
        SELECT id, symbol, quantity, entry_price, entry_time
        FROM trades
        WHERE status = 'OPEN'
    """)

    stale_positions = cursor.fetchall()

    if not stale_positions:
        print("No stale positions found.")
        return

    print("=" * 80)
    print("STALE POSITIONS DETECTED")
    print("=" * 80)

    for pos_id, symbol, qty, entry_price, entry_time in stale_positions:
        print(f"\nClosing: {symbol} x{qty} @ Rs{entry_price} (entered: {entry_time})")

        # Close it at entry price (neutral exit, no P&L impact)
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE trades
            SET exit_price = ?, pnl = 0, status = 'CLOSED',
                exit_time = ?, notes = 'EMERGENCY_CLEANUP'
            WHERE id = ?
        """, (entry_price, now, pos_id))

        print(f"  -> CLOSED (neutral P&L)")

    conn.commit()
    conn.close()

    print(f"\n✅ Cleaned up {len(stale_positions)} stale position(s)")


def verify_cleanup():
    """Verify no open positions remain."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cursor.fetchone()[0]
    conn.close()

    if count == 0:
        print("✅ Database clean: 0 stale positions")
        return True
    else:
        print(f"❌ Warning: {count} position(s) still OPEN")
        return False


if __name__ == "__main__":
    close_stale_positions()
    verify_cleanup()
