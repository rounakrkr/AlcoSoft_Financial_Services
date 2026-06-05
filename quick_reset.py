#!/usr/bin/env python3
"""
⚡ ALCOSOFT ONE-CLICK RESET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Super simple reset - no questions, automatic backup & cleanup.
Run this whenever you want a fresh slate.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime

def reset_everything():
    """One-shot reset without confirmations."""
    
    print("\n" + "="*70)
    print("🔄 ALCOSOFT AUTO-RESET")
    print("="*70 + "\n")
    
    try:
        # Backup
        print("💾 Creating backup...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = f"_backup_reset_{timestamp}"
        os.makedirs(backup_dir, exist_ok=True)
        
        backup_files = [
            "data/session_briefing.json", "data/positions.json", 
            "data/live_capital.json", "data/trading_session_state.json",
            "data/alcosoft.log"
        ]
        for file in backup_files:
            if os.path.exists(file):
                shutil.copy2(file, os.path.join(backup_dir, os.path.basename(file)))
        
        for db in ["data/alcosoft.db", "data/reflection.db", "data/reflection_statistics.db"]:
            if os.path.exists(db):
                shutil.copy2(db, os.path.join(backup_dir, os.path.basename(db)))
        
        print(f"   ✅ Backup: {backup_dir}\n")
        
        # Reset JSON files
        print("📄 Resetting JSON files...")
        json_defaults = {
            "data/session_briefing.json": {
                "generated_at": None, "session_type": "SAFE_FALLBACK",
                "market_bias": "NEUTRAL", "approved_stocks": [],
                "watchlist": [], "avoid_list": [],
            },
            "data/positions.json": [],
            "data/live_capital.json": {
                "initial_capital": 100000.0, "current_capital": 100000.0,
                "last_updated": datetime.now().isoformat(),
            },
            "data/trading_session_state.json": {
                "session_started": False, "session_date": None,
                "last_screener_run": None, "active_positions": 0, "total_pnl": 0.0,
            },
            "data/e2e_integration_results.json": [],
            "data/feed_stats.json": {},
            "data/instrument_tokens.json": {},
            "data/learnings.json": {
                "reflections": [], "observations": [], "last_updated": None,
            },
            "data/validation_results.json": [],
        }
        
        for path, content in json_defaults.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(content, f, indent=2)
            print(f"   ✅ {path}")
        
        # Delete databases
        print("\n🗄️  Clearing databases...")
        for db in ["data/alcosoft.db", "data/reflection.db", "data/reflection_statistics.db"]:
            if os.path.exists(db):
                os.remove(db)
                print(f"   ✅ {db}")
        
        # Clear log files
        print("\n📋 Clearing logs...")
        for log in ["data/alcosoft.log", "data/workflow_validation.log"]:
            if os.path.exists(log):
                with open(log, 'w') as f:
                    f.write("")
                print(f"   ✅ {log}")
        
        # Clear folders
        print("\n📁 Clearing folders...")
        for folder in ["data/audit", "data/cognition", "data/observations", 
                       "data/reflections", "data/reflection_snapshots"]:
            if os.path.exists(folder):
                shutil.rmtree(folder)
                os.makedirs(folder, exist_ok=True)
                print(f"   ✅ {folder}")
        
        print("\n" + "="*70)
        print("✅ ALL CLEAR! System ready for fresh start.")
        print("="*70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    reset_everything()
