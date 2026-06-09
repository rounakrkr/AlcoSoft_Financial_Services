import sys, os, asyncio
sys.path.insert(0, '/home/ubuntu/alcosoft')

from core.order_executor import get_capital_snapshot
from datetime import datetime, timedelta
import sqlite3

def test_margin_equity():
    snap = get_capital_snapshot()
    text_margin = (
        "📊 <b>RISK & MARGIN</b>\n\n"
        f"Mode: <code>{snap['mode']}</code>\n"
        f"Account Equity: ₹{snap['account_equity']:.2f}\n"
        f"Free Margin: ₹{snap['free_margin']:.2f}\n"
        f"Margin Blocked: ₹{snap['margin_blocked']:.2f}\n"
        f"Gross Exposure: ₹{snap['gross_exposure']:.2f}\n"
        f"Margin Utilized: {snap['margin_utilization']:.1f}%\n"
        f"Buying Power: ₹{snap['remaining_buying_power']:.2f}\n"
    )
    text_equity = (
        "💰 <b>EQUITY SNAPSHOT</b>\n\n"
        f"Starting Capital: ₹{snap['starting_capital']:.2f}\n"
        f"Closed PnL: ₹{snap['closed_pnl']:.2f}\n"
        f"Unrealized PnL: ₹{snap['unrealized_pnl']:.2f}\n"
        f"Total Equity: ₹{snap['account_equity']:.2f}\n"
    )
    print("=== /margin ===")
    print(text_margin)
    print("=== /equity ===")
    print(text_equity)

def test_week():
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    conn = sqlite3.connect('/home/ubuntu/alcosoft/data/alcosoft.db')
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT date, realized_equity, agent_decision_calls FROM daily_stats WHERE date >= ? ORDER BY date ASC", (start_of_week.isoformat(),)).fetchall()
    
    if not rows:
        print("📅 <b>WEEKLY SUMMARY</b>\nNo data for the current week yet.")
        return
        
    text = "📅 <b>WEEKLY SUMMARY</b>\n\n"
    total_pnl = 0.0
    total_trades = 0
    best_trade = 0.0
    worst_trade = 0.0
    
    trades_rows = conn.execute("SELECT pnl FROM trades WHERE status = 'CLOSED' AND exit_time >= ?", (start_of_week.isoformat(),)).fetchall()
    wins = 0
    losses = 0
    for tr in trades_rows:
        tpnl = float(tr['pnl'] or 0.0)
        if tpnl > 0: wins += 1
        else: losses += 1
        if tpnl > best_trade: best_trade = tpnl
        if tpnl < worst_trade: worst_trade = tpnl
        
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

    for r in rows:
        pnl = r['realized_equity'] or 0.0
        trades = r['agent_decision_calls'] or 0
        total_pnl += float(pnl)
        total_trades += int(trades)
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        text += f"{r['date']}: {icon} ₹{pnl:.2f} ({trades} trades)\n"
    
    text += f"\n<b>Total PnL:</b> ₹{total_pnl:.2f}\n"
    text += f"<b>Total Trades:</b> {total_trades}\n"
    text += f"<b>Win Rate:</b> {win_rate:.1f}%\n"
    text += f"<b>Best Trade:</b> ₹{best_trade:.2f}\n"
    text += f"<b>Worst Trade:</b> ₹{worst_trade:.2f}\n"
    
    print("=== /week ===")
    print(text)

if __name__ == '__main__':
    test_margin_equity()
    test_week()
