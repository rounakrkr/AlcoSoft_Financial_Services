import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path for imports
_ROOT = str(Path(__file__).parent.absolute())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

def reset_json_files():
    """Reset all JSON files to empty/default state."""
    print("\n📄 Resetting JSON files...")
    
    files_reset = {
        "data/session_briefing.json": {
            "generated_at": None,
            "session_type": "SAFE_FALLBACK",
            "market_bias": "NEUTRAL",
            "approved_stocks": [],
            "watchlist": [],
            "avoid_list": [],
        },
        "data/positions.json": [],
        "data/live_capital.json": {
            "initial_capital": 100000.0,
            "current_capital": 100000.0,
            "last_updated": datetime.now().isoformat(),
        },
        "data/trading_session_state.json": {
            "session_started": False,
            "session_date": None,
            "last_screener_run": None,
            "active_positions": 0,
            "total_pnl": 0.0,
        },
        "data/e2e_integration_results.json": [],
        "data/feed_stats.json": {},
        "data/instrument_tokens.json": {},
        "data/learnings.json": {
            "reflections": [],
            "observations": [],
            "last_updated": None,
        },
        "data/validation_results.json": [],
        "data/last_alert.json": {},
    }
    
    for file_path, default_content in files_reset.items():
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w') as f:
                json.dump(default_content, f, indent=2)
            print(f"  ✅ {file_path}")
        except Exception as e:
            print(f"  ⚠️  {file_path}: {e}")

def clear_databases():
    """Delete all database files and temp files."""
    print("\n🗄️  Clearing databases and temp files...")
    
    db_files = [
        "data/alcosoft.db",
        "data/reflection.db",
        "data/reflection_statistics.db",
        "data/market_close_report.json",
        "data/session_briefing_backup.json",
        "data/test_briefing.json",
        "data/yfinance_traces.json",
    ]
    
    for db_file in db_files:
        try:
            if os.path.exists(db_file):
                os.remove(db_file)
                print(f"  ✅ {db_file}")
            else:
                print(f"  ℹ️  {db_file} (not found)")
        except Exception as e:
            print(f"  ⚠️  {db_file}: {e}")

def clear_log_files():
    """Clear/truncate log files."""
    print("\n📋 Clearing log files...")
    
    log_files = [
        "data/alcosoft.log",
        "data/workflow_validation.log",
    ]
    
    for log_file in log_files:
        try:
            if os.path.exists(log_file):
                with open(log_file, 'w') as f:
                    f.write("")
                print(f"  ✅ {log_file}")
            else:
                print(f"  ℹ️  {log_file} (not found)")
        except Exception as e:
            print(f"  ⚠️  {log_file}: {e}")

def clear_data_folders():
    """Clear audit, cognition, observations, reflections folders."""
    print("\n📁 Clearing data folders...")
    
    folders_to_clear = [
        "data/audit",
        "data/cognition",
        "data/observations",
        "data/reflections",
        "data/reflection_snapshots",
    ]
    
    for folder in folders_to_clear:
        try:
            if os.path.exists(folder):
                shutil.rmtree(folder)
                os.makedirs(folder, exist_ok=True)
                print(f"  ✅ {folder}")
            else:
                print(f"  ℹ️  {folder} (not found)")
        except Exception as e:
            print(f"  ⚠️  {folder}: {e}")

def reinitialize_database():
    """Recreate database schema after clearing."""
    print("\n🗄️  Reinitializing database schema...")
    try:
        from core.state_manager import initialize_db
        initialize_db()
        print("  ✅ Database schema reinitialized")
    except Exception as e:
        print(f"  ⚠️  Database reinitialization failed: {e}")

def main():
    print("=" * 70)
    print("🔄 CLOUD AUTO-RESET STARTED")
    print("=" * 70)
    
    # Perform reset silently without backup
    reset_json_files()
    clear_databases()
    reinitialize_database()
    clear_log_files()
    clear_data_folders()
    
    print("\n✅ CLOUD SYSTEM RESET COMPLETE")

if __name__ == "__main__":
    main()
