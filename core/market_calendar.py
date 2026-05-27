# ============================================================
#   ALCOSOFT — NSE trading calendar (weekends + holidays)
# ============================================================

from datetime import date, datetime, time as dt_time

# NSE equity segment holidays (update yearly)
NSE_HOLIDAYS: frozenset[str] = frozenset({
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
    "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15",
    "2025-08-27", "2025-10-02", "2025-10-21", "2025-10-22",
    "2025-11-05", "2025-12-25",
    # 2026
    "2026-01-26", "2026-02-26", "2026-03-03", "2026-03-31",
    "2026-04-02", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-08-15", "2026-10-02",
    "2026-10-20", "2026-10-21", "2026-11-10", "2026-12-25",
})

MARKET_OPEN  = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)
PRE_MARKET_START = dt_time(8, 15)


def is_trading_day(d: date | None = None) -> bool:
    """False on weekends and NSE holidays."""
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in NSE_HOLIDAYS


def is_market_session_open(now: datetime | None = None) -> bool:
    """True during regular NSE cash session on a trading day."""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_pre_market(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return PRE_MARKET_START <= t < MARKET_OPEN


def market_status_message(now: datetime | None = None) -> tuple[bool, str]:
    """For health checks — (ok_to_run, message)."""
    now = now or datetime.now()
    d = now.date()
    t = now.time()

    if not is_trading_day(d):
        if d.weekday() >= 5:
            return False, f"Weekend — market closed ({d.strftime('%A')})"
        return False, f"NSE holiday — market closed ({d.isoformat()})"

    if is_market_session_open(now):
        return True, "Market is OPEN"

    if is_pre_market(now):
        return True, "Pre-market (screener will run soon)"

    return False, f"Market closed (open 9:15-15:30, now {t.strftime('%H:%M')})"
