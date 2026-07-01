# -*- coding: utf-8 -*-
"""
core/regime_filter.py
─────────────────────
Market Regime Filter — Dual Engine Edition

Dynamically pulls parameters from trading_settings.json
Calculates both Bull and Bear regimes based on Nifty 50 gap breadth.
"""

import logging
from datetime import date
from typing import Optional

from core import trading_settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Module-level cache (persists for the trading day)
# ─────────────────────────────────────────────────────────────
_cache_date: Optional[date] = None
_cache_bull: bool = False
_cache_bear: bool = False


def is_bull_day() -> bool:
    """Returns True if today qualifies as a strong-gap bull day."""
    _ensure_computed()
    return _cache_bull


def is_bear_day() -> bool:
    """Returns True if today qualifies as a strong-gap bear day."""
    _ensure_computed()
    return _cache_bear


def _ensure_computed():
    global _cache_date, _cache_bull, _cache_bear
    today = date.today()
    if _cache_date == today:
        return
    
    bull_res, bear_res = _compute_regimes()
    _cache_date = today
    _cache_bull = bull_res
    _cache_bear = bear_res

    # Send alerts
    try:
        from core.alerts import NotificationService
        from datetime import datetime
        today_str = datetime.now().strftime("%d %b %Y")
        
        if bull_res:
            msg = f"🟢 <b>BULL REGIME CONFIRMED — {today_str}</b>\nUniverse gap up threshold met. Long Engine ENABLED."
            logger.info("[RegimeFilter] BULL REGIME CONFIRMED — Long Engine ENABLED.")
            NotificationService.broadcast(msg, priority="HIGH")
        elif bear_res:
            msg = f"🔴 <b>BEAR REGIME CONFIRMED — {today_str}</b>\nUniverse gap down threshold met. Short Engine ENABLED."
            logger.info("[RegimeFilter] BEAR REGIME CONFIRMED — Short Engine ENABLED.")
            NotificationService.broadcast(msg, priority="HIGH")
        else:
            msg = f"⚪ <b>NO REGIME — {today_str}</b>\nMarket is sideways. Both engines BLOCKED."
            logger.info("[RegimeFilter] NO REGIME — Market is sideways. Both engines BLOCKED.")
            NotificationService.broadcast(msg, priority="HIGH")
    except Exception as e:
        logger.warning(f"[RegimeFilter] Telegram alert failed: {e}")


def _compute_regimes() -> tuple[bool, bool]:
    """
    Check today's open vs yesterday's close for all Nifty50 stocks.
    Returns (is_bull, is_bear)
    """
    try:
        bull_gap_pct    = trading_settings.get("risk", "regime_bull_gap_pct",      0.006)
        bear_gap_pct    = trading_settings.get("risk", "regime_bear_gap_pct",     -0.006)
        bull_breadth_pct = trading_settings.get("risk", "regime_bull_breadth_pct", 0.30)
        bear_breadth_pct = trading_settings.get("risk", "regime_bear_breadth_pct", 0.50)

        from screener.morning_screener import MIDCAP_50, _fetch_yahoo_history

        bull_count = 0
        bear_count = 0
        total_count = 0

        for symbol in MIDCAP_50:
            try:
                df = _fetch_yahoo_history(symbol, period="5d", interval="1d")
                if df is None or df.empty or len(df) < 2:
                    continue
                df.columns = [c.lower() for c in df.columns]
                df.dropna(subset=["close", "open"], inplace=True)
                if len(df) < 2:
                    continue

                prev_close = float(df["close"].iloc[-2])
                today_open = float(df["open"].iloc[-1])

                if prev_close <= 0:
                    continue

                gap_pct = (today_open - prev_close) / prev_close
                total_count += 1
                
                if gap_pct >= bull_gap_pct:
                    bull_count += 1
                elif gap_pct <= bear_gap_pct:
                    bear_count += 1

            except Exception:
                continue

        if total_count == 0:
            logger.warning("[RegimeFilter] No data fetched — failing CLOSED (NO REGIME, both engines blocked).")
            return False, False

        bull_ratio = bull_count / total_count
        bear_ratio = bear_count / total_count

        logger.info(
            f"[RegimeFilter] Bull Breadth: {bull_ratio*100:.1f}% (need {bull_breadth_pct*100:.0f}%), "
            f"Bear Breadth: {bear_ratio*100:.1f}% (need {bear_breadth_pct*100:.0f}%)"
        )

        is_bull = bull_ratio >= bull_breadth_pct
        is_bear = bear_ratio >= bear_breadth_pct

        # Can't be both. If somehow both meet threshold (impossible mathematically if threshold >= 0.5, but just in case)
        if is_bull and is_bear:
            is_bear = False

        return is_bull, is_bear

    except Exception as e:
        logger.error(f"[RegimeFilter] Computation error: {e} — failing CLOSED (NO REGIME).")
        return False, False


def force_bull(value: bool = True):
    global _cache_date, _cache_bull, _cache_bear
    _cache_date = date.today()
    _cache_bull = value
    _cache_bear = False
    logger.warning(f"[RegimeFilter] MANUAL OVERRIDE: bull forced to {value}")

def force_bear(value: bool = True):
    global _cache_date, _cache_bull, _cache_bear
    _cache_date = date.today()
    _cache_bull = False
    _cache_bear = value
    logger.warning(f"[RegimeFilter] MANUAL OVERRIDE: bear forced to {value}")
