import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number


def clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    number = safe_float(value, default)
    return max(minimum, min(maximum, number))


def safe_read_json(
    path: str | os.PathLike,
    default: Any,
    *,
    expected_type: type | tuple[type, ...] | None = None,
    label: str | None = None,
    log: logging.Logger | None = None,
) -> Any:
    active_log = log or logger
    file_path = Path(path)
    display = label or str(file_path)

    if not file_path.exists():
        return default

    try:
        raw = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        active_log.warning("Could not read %s: %s; using fallback", display, exc)
        return default

    if not raw.strip():
        active_log.warning("%s is empty; using fallback", display)
        return default

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        active_log.error("Malformed JSON in %s: %s; using fallback", display, exc)
        return default

    if expected_type and not isinstance(data, expected_type):
        active_log.error(
            "%s has invalid JSON shape %s; expected %s; using fallback",
            display,
            type(data).__name__,
            expected_type,
        )
        return default

    return data


def atomic_write_json(
    path: str | os.PathLike,
    data: Any,
    *,
    indent: int = 2,
    label: str | None = None,
    log: logging.Logger | None = None,
) -> bool:
    active_log = log or logger
    file_path = Path(path)
    display = label or str(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(file_path.parent),
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, indent=indent, ensure_ascii=False)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())

        os.replace(tmp_name, file_path)
        return True
    except Exception as exc:
        active_log.error("Failed to write %s atomically: %s", display, exc)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False


def safe_call(
    label: str,
    fn: Callable,
    *args,
    default: Any = None,
    log: logging.Logger | None = None,
    **kwargs,
) -> Any:
    active_log = log or logger
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        active_log.warning("%s failed: %s", label, exc, exc_info=True)
        return default
