import os
import sys
import sqlite3
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.state_manager import (
    _get_conn, initialize_db, initialize_daily_capital, _update_daily_stats,
    get_today_stats
)

today = datetime.now().strftime("%Y-%m-%d")

def reset_db():
    with _get_conn() as conn:
        conn.execute("DELETE FROM daily_stats WHERE date = ?", (today,))
        conn.execute("DELETE FROM trades WHERE date = ?", (today,))

def test_fx06():
    print("\n--- Test FX06: Initial Capital Initialization ---\n")
    initialize_db()

    # 1. Paper Mode
    reset_db()
    with patch('os.getenv', side_effect=lambda k, d: "PAPER" if k == "TRADING_MODE" else d):
        with patch('core.trading_settings.get', return_value=50000.0):
            initialize_daily_capital()
            stats = get_today_stats()
            print(f"Paper Mode capital_start: {stats.get('capital_start')}")
            assert stats.get('capital_start') == 50000.0

    # 2. Live Mode (Normal Startup)
    reset_db()
    with patch('os.getenv', side_effect=lambda k, d: "LIVE" if k == "TRADING_MODE" else d):
        with patch('core.order_executor._get_available_capital', return_value=25000.0):
            with patch('core.order_executor.is_capital_fresh', return_value=True):
                with patch('core.state_manager.get_today_gross_pnl', return_value=0.0):
                    with patch('core.state_manager.get_open_positions', return_value=[]):
                        with patch('core.market_calendar.is_trading_day', return_value=True):
                            with patch('core.state_manager.datetime') as mock_dt:
                                mock_dt.now.return_value = datetime.now().replace(hour=9, minute=0)
                                initialize_daily_capital()
                                stats = get_today_stats()
                                print(f"Live Normal capital_start: {stats.get('capital_start')}")
                                assert stats.get('capital_start') == 25000.0

    # 3. Live Mode (API Failure)
    reset_db()
    with patch('os.getenv', side_effect=lambda k, d: "LIVE" if k == "TRADING_MODE" else d):
        with patch('core.order_executor._get_available_capital', side_effect=Exception("API Down")):
            with patch('core.state_manager.get_open_positions', return_value=[]):
                initialize_daily_capital()
                stats = get_today_stats()
                print(f"API Failure capital_start: {stats.get('capital_start')}")
                assert stats.get('capital_start') is None

    # 4. Startup with Open Positions
    reset_db()
    with patch('os.getenv', side_effect=lambda k, d: "LIVE" if k == "TRADING_MODE" else d):
        with patch('core.state_manager.get_open_positions', return_value=[{"symbol": "TEST"}]):
            initialize_daily_capital()
            stats = get_today_stats()
            print(f"Open Positions Startup capital_start: {stats.get('capital_start')}")
            assert stats.get('capital_start') is None

    # 5. Retry after becoming flat
    reset_db()
    with patch('os.getenv', side_effect=lambda k, d: "LIVE" if k == "TRADING_MODE" else d):
        # Initial startup skipped due to open positions
        with patch('core.state_manager.get_open_positions', return_value=[{"symbol": "TEST"}]):
            initialize_daily_capital()
            assert get_today_stats().get('capital_start') is None
        
        # Now a position closes, system becomes flat.
        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO trades (symbol, action, quantity, entry_price, status, date, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("TEST", "BUY", 10, 100, "CLOSED", today, 500.0))
        
        # _update_daily_stats runs.
        # It calls get_open_positions (now empty), and _get_available_capital (now 25500)
        with patch('core.state_manager.get_open_positions', return_value=[]):
            with patch('core.order_executor._get_available_capital', return_value=25500.0):
                with patch('core.order_executor.is_capital_fresh', return_value=True):
                    with patch('core.state_manager.get_today_gross_pnl', return_value=500.0):
                        with patch('core.market_calendar.is_trading_day', return_value=True):
                            with patch('core.state_manager.datetime') as mock_dt:
                                mock_dt.now.return_value = datetime.now().replace(hour=10, minute=0)
                                _update_daily_stats()
                    
        stats = get_today_stats()
        print(f"Retry Flat capital_start: {stats.get('capital_start')}")
        assert stats.get('capital_start') == 25000.0  # 25500 - 500

    print("\n✅ All FX06 Tests Passed")

if __name__ == "__main__":
    test_fx06()
