import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from core.safe_io import safe_read_json


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategySetDefinition:
    side: str
    name: str
    conditions: tuple[str, ...]
    priority: int = 100
    base_confidence: float = 70.0
    confidence_weight: float = 1.0
    notes: str = ""


@dataclass(frozen=True)
class StrategySetConfig:
    buy_sets: tuple[StrategySetDefinition, ...]
    sell_sets: tuple[StrategySetDefinition, ...]


_cache_lock = threading.Lock()
_cache: dict[Path, tuple[int | None, StrategySetConfig]] = {}


def _default_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "config" / "strategy_sets.json"


def strategy_sets_path() -> Path:
    configured = os.getenv("STRATEGY_SETS_PATH")
    return Path(configured) if configured else _default_path()


def normalize_set_key(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _coerce_float(
    item: dict,
    key: str,
    default: float,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    raw = item.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc

    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _parse_conditions(item: dict, side: str, idx: int, name: str) -> tuple[str, ...]:
    conditions = item.get("conditions", [])
    if not isinstance(conditions, list):
        raise ValueError(f"{side}_sets[{idx}] {name} conditions must be a list")

    parsed = tuple(str(c).strip() for c in conditions if str(c).strip())
    if not parsed:
        raise ValueError(f"{side}_sets[{idx}] {name} must define at least one condition")
    return parsed


def _parse_sets(raw_sets: list[dict], side: str) -> tuple[StrategySetDefinition, ...]:
    if raw_sets is None:
        return tuple()
    if not isinstance(raw_sets, list):
        raise ValueError(f"{side}_sets must be a list")

    parsed: list[StrategySetDefinition] = []
    seen_names: set[str] = set()

    for idx, item in enumerate(raw_sets):
        if not isinstance(item, dict):
            raise ValueError(f"{side}_sets[{idx}] must be an object")

        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"{side}_sets[{idx}] is missing name")

        set_key = normalize_set_key(name)
        if set_key in seen_names:
            raise ValueError(f"Duplicate {side} strategy set name: {name}")
        seen_names.add(set_key)

        conditions = _parse_conditions(item, side, idx, name)

        parsed.append(
            StrategySetDefinition(
                side=side,
                name=name,
                conditions=conditions,
                priority=int(item.get("priority", 100)),
                base_confidence=_coerce_float(
                    item,
                    "base_confidence",
                    70.0,
                    0.0,
                    100.0,
                    f"{name}.base_confidence",
                ),
                confidence_weight=_coerce_float(
                    item,
                    "confidence_weight",
                    1.0,
                    0.1,
                    2.0,
                    f"{name}.confidence_weight",
                ),
                notes=str(item.get("notes", "")).strip(),
            )
        )

    return tuple(sorted(parsed, key=lambda s: (s.priority, s.name)))


def _read_strategy_sets(config_path: Path) -> StrategySetConfig:
    raw = safe_read_json(
        config_path,
        {},
        expected_type=dict,
        label="strategy set config",
        log=logger,
    )

    if not isinstance(raw, dict):
        logger.error("strategy set config must be a JSON object; disabling new entries")
        raw = {}

    return StrategySetConfig(
        buy_sets=_parse_sets(raw.get("buy_sets", []), "buy"),
        sell_sets=_parse_sets(raw.get("sell_sets", []), "sell"),
    )


def load_strategy_sets(path: str | os.PathLike | None = None) -> StrategySetConfig:
    config_path = (Path(path) if path else strategy_sets_path()).resolve()
    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None

    with _cache_lock:
        cached = _cache.get(config_path)
        if cached and cached[0] == mtime_ns:
            return cached[1]

        try:
            config = _read_strategy_sets(config_path)
        except Exception as exc:
            if cached:
                logger.error("Strategy set reload failed; keeping last good config: %s", exc)
                return cached[1]
            logger.error("Strategy set config invalid; disabling strategy sets: %s", exc)
            config = StrategySetConfig(buy_sets=tuple(), sell_sets=tuple())
        _cache[config_path] = (mtime_ns, config)
        return config


def clear_strategy_set_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_strategy_set_names(side: str | None = None) -> list[str]:
    config = load_strategy_sets()
    if side == "buy":
        return [s.name for s in config.buy_sets]
    if side == "sell":
        return [s.name for s in config.sell_sets]
    return [s.name for s in config.buy_sets + config.sell_sets]
