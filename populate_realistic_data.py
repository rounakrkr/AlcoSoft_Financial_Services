#!/usr/bin/env python3
"""
Populate realistic trading data for LinkedIn demo.
Starting capital: ₹10,000
6-7 closed trades, 2-3 open positions
Realistic P&L and win rate
"""

import sqlite3
import json
import os
from datetime import datetime, timedelta

DB_PATH = "data/alcosoft.db"
TODAY = "2026-05-28"

# Realistic closed trades data - 67% win rate (4W, 2L out of 6 trades)
CLOSED_TRADES = [
    {
        "symbol": "HDFCBANK",
        "qty": 3,
        "action": "BUY",
        "entry_price": 1542.50,
        "entry_time": "2026-05-28T09:30:15",
        "exit_price": 1568.85,
        "exit_time": "2026-05-28T09:58:30",
        "pnl": 79.05,
        "pnl_percent": 1.71,
        "status": "CLOSED",
        "strategy": "WAR_ROOM",
        "date": TODAY,
    },
    {
        "symbol": "ONGC",
        "qty": 6,
        "action": "BUY",
        "entry_price": 192.30,
        "entry_time": "2026-05-28T10:15:40",
        "exit_price": 199.15,
        "exit_time": "2026-05-28T11:08:22",
        "pnl": 41.10,
        "pnl_percent": 3.56,
        "status": "CLOSED",
        "strategy": "MATH_WATCHLIST",
        "date": TODAY,
    },
    {
        "symbol": "TATASTEEL",
        "qty": 7,
        "action": "BUY",
        "entry_price": 131.20,
        "entry_time": "2026-05-28T11:22:10",
        "exit_price": 129.45,
        "exit_time": "2026-05-28T12:15:45",
        "pnl": -12.25,
        "pnl_percent": -0.93,
        "status": "CLOSED",
        "strategy": "MATH_WATCHLIST",
        "date": TODAY,
    },
    {
        "symbol": "NTPC",
        "qty": 8,
        "action": "BUY",
        "entry_price": 282.15,
        "entry_time": "2026-05-28T12:30:55",
        "exit_price": 290.45,
        "exit_time": "2026-05-28T13:05:30",
        "pnl": 65.60,
        "pnl_percent": 2.92,
        "status": "CLOSED",
        "strategy": "MATH_WATCHLIST",
        "date": TODAY,
    },
    {
        "symbol": "MARUTI",
        "qty": 1,
        "action": "BUY",
        "entry_price": 9610.75,
        "entry_time": "2026-05-28T13:15:20",
        "exit_price": 9505.50,
        "exit_time": "2026-05-28T14:10:00",
        "pnl": -105.25,
        "pnl_percent": -1.09,
        "status": "CLOSED",
        "strategy": "TECHNICAL",
        "date": TODAY,
    },
    {
        "symbol": "COALINDIA",
        "qty": 5,
        "action": "BUY",
        "entry_price": 241.30,
        "entry_time": "2026-05-28T14:20:35",
        "exit_price": 250.95,
        "exit_time": "2026-05-28T14:55:10",
        "pnl": 48.25,
        "pnl_percent": 4.00,
        "status": "CLOSED",
        "strategy": "TECHNICAL",
        "date": TODAY,
    },
]

# Realistic open positions
OPEN_POSITIONS = [
    {
        "symbol": "ICICIBANK",
        "qty": 4,
        "action": "BUY",
        "entry_price": 686.50,
        "entry_time": "2026-05-28T10:45:15",
        "current_price": 701.25,
        "pnl": 58.75,
        "pnl_percent": 2.14,
        "status": "OPEN",
        "strategy": "WAR_ROOM",
        "stop_loss": 650.00,
        "target": 720.00,
        "date": TODAY,
    },
    {
        "symbol": "INFY",
        "qty": 2,
        "action": "BUY",
        "entry_price": 1458.20,
        "entry_time": "2026-05-28T11:30:40",
        "current_price": 1418.50,
        "pnl": -79.40,
        "pnl_percent": -2.72,
        "status": "OPEN",
        "strategy": "TECHNICAL",
        "stop_loss": 1400.00,
        "target": 1520.00,
        "date": TODAY,
    },
    {
        "symbol": "TCS",
        "qty": 3,
        "action": "BUY",
        "entry_price": 3512.40,
        "entry_time": "2026-05-28T12:45:20",
        "current_price": 3548.75,
        "pnl": 109.05,
        "pnl_percent": 1.01,
        "status": "OPEN",
        "strategy": "MATH_WATCHLIST",
        "stop_loss": 3475.00,
        "target": 3650.00,
        "date": TODAY,
    },
]

def init_db():
    """Initialize database with realistic data"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Clear existing trades for today
    cursor.execute("DELETE FROM trades WHERE date = ?", (TODAY,))
    cursor.execute("DELETE FROM daily_stats WHERE date = ?", (TODAY,))
    
    # Insert closed trades
    for trade in CLOSED_TRADES:
        cursor.execute("""
            INSERT INTO trades 
            (date, symbol, action, quantity, entry_price, entry_time, exit_price, exit_time, 
             pnl, status, strategy, stop_loss, target_price, trading_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade["date"], trade["symbol"], trade["action"],
            trade["qty"], trade["entry_price"], trade["entry_time"],
            trade["exit_price"], trade["exit_time"],
            trade["pnl"], trade["status"], trade["strategy"],
            0.0, trade.get("target", 0), "PAPER"
        ))
    
    # Calculate stats
    win_count = sum(1 for t in CLOSED_TRADES if t["pnl"] >= 0)
    loss_count = sum(1 for t in CLOSED_TRADES if t["pnl"] < 0)
    total_pnl = sum(t["pnl"] for t in CLOSED_TRADES)
    
    # Insert daily stats
    cursor.execute("""
        INSERT INTO daily_stats
        (date, total_trades, winning_trades, losing_trades, gross_pnl, capital_start, capital_end)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        TODAY, len(CLOSED_TRADES), win_count, loss_count,
        total_pnl, 10000.0, 10000.0 + total_pnl
    ))
    
    conn.commit()
    conn.close()
    print(f"✓ Database populated with {len(CLOSED_TRADES)} closed trades")

def update_json_files():
    """Update JSON files with matching data"""
    
    # trades_history.json
    trades_history = [{
        "symbol": t["symbol"],
        "qty": t["qty"],
        "entry_price": t["entry_price"],
        "entry_time": t["entry_time"],
        "exit_price": t["exit_price"],
        "exit_time": t["exit_time"],
        "strategy": t["strategy"],
        "type": "LONG",
        "status": "CLOSED",
        "pnl": t["pnl"],
        "pnl_percent": t["pnl_percent"],
        "reason": "Target hit" if t["pnl"] >= 0 else "Stop loss hit"
    } for t in CLOSED_TRADES]
    
    with open("data/trades_history.json", "w") as f:
        json.dump(trades_history, f, indent=2)
    
    # positions.json
    positions_data = [{
        "symbol": p["symbol"],
        "qty": p["qty"],
        "entry_price": p["entry_price"],
        "entry_time": p["entry_time"],
        "current_price": p["current_price"],
        "strategy": p["strategy"],
        "type": "LONG",
        "status": "OPEN",
        "pnl": p["pnl"],
        "pnl_percent": p["pnl_percent"],
        "stop_loss": p["stop_loss"],
        "target": p["target"]
    } for p in OPEN_POSITIONS]
    
    with open("data/positions.json", "w") as f:
        json.dump(positions_data, f, indent=2)
    
    # Calculate current capital
    closed_pnl = sum(t["pnl"] for t in CLOSED_TRADES)
    open_pnl = sum(p["pnl"] for p in OPEN_POSITIONS)
    current_capital = 10000 + closed_pnl + open_pnl
    
    # live_capital.json
    capital_data = {
        "capital": current_capital,
        "timestamp": datetime.now().isoformat()
    }
    
    with open("data/live_capital.json", "w") as f:
        json.dump(capital_data, f, indent=2)
    
    print(f"✓ JSON files updated")
    print(f"  - Closed P&L: ₹{closed_pnl:.2f}")
    print(f"  - Open P&L: ₹{open_pnl:.2f}")
    print(f"  - Current Capital: ₹{current_capital:.2f}")

if __name__ == "__main__":
    try:
        init_db()
        update_json_files()
        
        # Calculate and display stats
        win_count = sum(1 for t in CLOSED_TRADES if t["pnl"] > 0)
        loss_count = sum(1 for t in CLOSED_TRADES if t["pnl"] < 0)
        total_pnl = sum(t["pnl"] for t in CLOSED_TRADES)
        
        print(f"\n📊 Trading Summary:")
        print(f"  Wins: {win_count}/7 ({win_count*100//7}%)")
        print(f"  Losses: {loss_count}/7 ({loss_count*100//7}%)")
        print(f"  Total P&L: ₹{total_pnl:.2f}")
        print(f"\n✅ Data ready for LinkedIn demo!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
