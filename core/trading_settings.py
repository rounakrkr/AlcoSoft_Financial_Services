# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/trading_settings.py — Runtime trading config (not .env)
#   Loaded from config/trading_settings.json
#   Editable from dashboard; hot-reloads when file changes.
# ============================================================

import copy
import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(_ROOT, "config", "trading_settings.json")

_lock = threading.Lock()
_cache: dict | None = None
_mtime: float | None = None

DEFAULTS: dict = {
    "risk": {
        "stop_loss_percent": 0.01,
        "trailing_sl_percent": 0.008,
        "target_rr_ratio": 2.0,
        "max_risk_per_trade": 0.02,
        "math_risk_per_trade": 0.05,
        "max_daily_loss_percent": 0.05,
        "paper_capital": 10000,
        "allow_margin": False,
        "forced_buy_margin": False,
        "margin_leverage": 2.0,
        "position_size_margin": 0.75,
    },
    "strategy": {
        "strategy_type": "INTRADAY",
        "max_open_positions": 2,
        "min_confidence": 70,
        "signal_lookback_candles": 3,
        "min_ws_candles_for_patterns": 2,
        "loop_interval_sec": 5,
    },
    "screener": {
        "screener_total_stocks": 8,
        "cognition_picks": 8,
    },
    "market_data": {
        "candle_interval_seconds": 300,
        "health_min_ws_candles": 4,
        "scan_log_interval_sec": 90,
    },
    "scheduling": {
        "cognition_cycle_interval_minutes": 30,
    },
    "adaptive": {
        "last_updated": None,
        "strategy": {
            "signal_confidence_multipliers": {},
            "market_regime_multiplier": 1.0,
        },
        "time_windows": {},
        "symbol_stops": {},
    },
}

# UI schema for dashboard forms
FIELD_SCHEMA: list[dict] = [
    {"section": "risk", "key": "stop_loss_percent", "label": "Stop loss (%)", "type": "percent", "min": 0.1, "max": 10, "step": 0.1, "hint": "Hard SL distance below entry (e.g. 1 = 1%)"},
    {"section": "risk", "key": "trailing_sl_percent", "label": "Trailing SL (%)", "type": "percent", "min": 0.1, "max": 5, "step": 0.1, "hint": "Trail distance from peak price"},
    {"section": "risk", "key": "target_rr_ratio", "label": "Profit target (R:R)", "type": "float", "min": 1, "max": 5, "step": 0.1, "hint": "Target = risk × this ratio"},
    {"section": "risk", "key": "max_risk_per_trade", "label": "AI agent risk / trade (%)", "type": "percent", "min": 0.5, "max": 10, "step": 0.5, "hint": "Max capital % risked per AI agent trade (cognitive signals)"},
    {"section": "risk", "key": "math_risk_per_trade", "label": "Math watchlist risk / trade (%)", "type": "percent", "min": 0.5, "max": 10, "step": 0.5, "hint": "Max capital % risked per math/technical trade (slower orders)"},
    {"section": "risk", "key": "max_daily_loss_percent", "label": "Max daily loss (%)", "type": "percent", "min": 1, "max": 20, "step": 0.5, "hint": "Circuit breaker — stops ALL trading if daily P&L falls below this"},
    {"section": "risk", "key": "paper_capital", "label": "Paper capital (₹)", "type": "int", "min": 1000, "max": 10000000, "step": 1000, "hint": "Total bankroll available for trading (used for position sizing)"},
    {"section": "risk", "key": "allow_margin", "label": "🔴 Allow margin", "type": "bool", "hint": "⚠️ Turn this ON to use broker margin. Turn it OFF to trade with real capital only (recommended for safe mode)."},
    {"section": "risk", "key": "forced_buy_margin", "label": "🔥 Force buy with margin", "type": "bool", "hint": "⚠️ AGGRESSIVE: If enabled + Allow margin ON, buys maximum with 100% margin even if risk calc says 0."},
    {"section": "risk", "key": "margin_leverage", "label": "Margin leverage / buying power", "type": "float", "min": 1.0, "max": 5.0, "step": 0.5, "hint": "Choose how much extra buying power to unlock. 2.0 = 2x capital, 3.0 = 3x capital. Values above 5.0 are rejected."},
    {"section": "risk", "key": "position_size_margin", "label": "Margin position size (%)", "type": "percent", "min": 10, "max": 100, "step": 5, "hint": "Choose what percentage of the margin buying power is used per trade. 100% uses all available margin power. Values above 100% are rejected."},
    {"section": "strategy", "key": "max_open_positions", "label": "Max open positions", "type": "int", "min": 1, "max": 10, "step": 1, "hint": "Max simultaneous open positions (affects capital allocation)."},
    {"section": "strategy", "key": "min_confidence", "label": "Min AI confidence", "type": "int", "min": 0, "max": 100, "step": 5, "hint": "Minimum confidence (%) required for AI agent picks."},
    {"section": "strategy", "key": "signal_lookback_candles", "label": "Signal lookback (candles)", "type": "int", "min": 1, "max": 10, "step": 1, "hint": "How many past candles to scan for patterns"},
    {"section": "strategy", "key": "min_ws_candles_for_patterns", "label": "Min live WS candles (patterns)", "type": "int", "min": 1, "max": 10, "step": 1, "hint": "Minimum live websocket candles required to detect patterns."},
    {"section": "strategy", "key": "loop_interval_sec", "label": "Strategy loop interval (sec)", "type": "int", "min": 1, "max": 60, "step": 1, "hint": "Seconds between each strategy loop iteration."},
    {"section": "screener", "key": "screener_total_stocks", "label": "Screener: total stocks", "type": "int", "min": 5, "max": 50, "step": 1, "hint": "Total number of stocks the screener scans each run."},
    {"section": "screener", "key": "cognition_picks", "label": "Screener: cognition picks", "type": "int", "min": 1, "max": 10, "step": 1, "hint": "Number of stocks selected for cognitive analysis each morning."},
    {"section": "market_data", "key": "candle_interval_seconds", "label": "Candle size (seconds)", "type": "int", "min": 60, "max": 900, "step": 60, "hint": "300 = 5 minutes"},
    {"section": "market_data", "key": "health_min_ws_candles", "label": "Health check: min WS candles", "type": "int", "min": 1, "max": 30, "step": 1, "hint": "Minimum websocket candles to consider data healthy."},
    {"section": "market_data", "key": "scan_log_interval_sec", "label": "Full scan log interval (sec)", "type": "int", "min": 30, "max": 600, "step": 10, "hint": "Seconds between writing full scan logs."},
    {"section": "scheduling", "key": "cognition_cycle_interval_minutes", "label": "Cognition cycle interval (min)", "type": "int", "min": 5, "max": 120, "step": 5, "hint": "Minutes between cognition cycles."},
]


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for section, values in override.items():
        if section.startswith("_"):
            out[section] = values
            continue
        if not isinstance(values, dict):
            continue
        out.setdefault(section, {})
        out[section].update(values)
    return out


def load_settings(force: bool = False) -> dict:
    global _cache, _mtime
    path = SETTINGS_PATH

    with _lock:
        try:
            mtime = os.path.getmtime(path) if os.path.exists(path) else None
        except OSError:
            mtime = None

        if not force and _cache is not None and mtime == _mtime:
            return _cache

        data = copy.deepcopy(DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                data = _deep_merge(data, raw)
            except Exception as e:
                logger.error(f"Failed to load {path}: {e}")

        data["_meta"] = {
            **data.get("_meta", {}),
            "loaded_at": datetime.now().isoformat(),
            "path": path,
        }
        _cache = data
        _mtime = mtime
        return _cache


def save_settings(updates: dict) -> dict:
    """Merge section updates and write JSON file."""
    current = load_settings(force=True)
    merged = _deep_merge(current, updates)
    merged["_meta"] = {
        "version": 1,
        "updated_at": datetime.now().isoformat(),
        "updated_via": "dashboard",
    }

    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    global _cache, _mtime
    with _lock:
        _cache = merged
        try:
            _mtime = os.path.getmtime(SETTINGS_PATH)
        except OSError:
            _mtime = None

    logger.info("Trading settings saved to %s", SETTINGS_PATH)
    return merged


def get(section: str, key: str, default: Any = None) -> Any:
    return load_settings().get(section, {}).get(key, default)


def get_section(section: str) -> dict:
    return dict(load_settings().get(section, {}))


def validate_updates(updates: dict) -> tuple[dict, list[str]]:
    """Validate and coerce dashboard POST body → sectioned dict."""
    errors: list[str] = []
    cleaned: dict = {}

    field_map = {(f["section"], f["key"]): f for f in FIELD_SCHEMA}

    for section, values in updates.items():
        if section.startswith("_") or not isinstance(values, dict):
            continue
        cleaned[section] = {}
        for key, raw in values.items():
            spec = field_map.get((section, key))
            if not spec:
                continue
            try:
                cleaned[section][key] = _coerce(spec, raw)
            except ValueError as e:
                errors.append(str(e))

    return cleaned, errors


def _coerce(spec: dict, raw: Any) -> Any:
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "true", "yes", "on")

    if t == "int":
        v = int(float(raw))
        _range_check(spec, v)
        return v

    if t == "percent":
        v = float(raw)
        if v > 1 and v <= 100:
            v = v / 100.0
        _range_check(spec, v * 100 if spec.get("max", 1) > 1 else v)
        return round(v, 6)

    if t == "float":
        v = float(raw)
        _range_check(spec, v)
        return round(v, 6)

    return raw


def _range_check(spec: dict, v: float):
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"{spec['label']}: min {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{spec['label']}: max {hi}")
