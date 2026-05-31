import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ADVISORY_DELTA_CAP = 0.05


@dataclass(frozen=True)
class ExecutionAdvisory:
    confidence_multiplier: float = 1.0
    market_multiplier: float = 1.0
    reason: str = "neutral"
    source: str = "none"

    def as_dict(self) -> dict:
        return {
            "confidence_multiplier": self.confidence_multiplier,
            "market_multiplier": self.market_multiplier,
            "reason": self.reason,
            "source": self.source,
        }


def _clamp_delta(value: float) -> float:
    return max(-ADVISORY_DELTA_CAP, min(ADVISORY_DELTA_CAP, value))


def _multiplier_from_delta(delta: float) -> float:
    return round(1.0 + _clamp_delta(delta), 4)


def _latest_cognition_hint() -> tuple[float, list[str]]:
    try:
        from reflection.cognition_engine import load_recent_cognition_cycles

        cycles = load_recent_cognition_cycles(limit=3)
    except Exception as exc:
        logger.debug("Cognition advisory unavailable: %s", exc)
        return 0.0, []

    if not cycles:
        return 0.0, []

    delta = 0.0
    reasons: list[str] = []
    latest = cycles[-1]
    text = " ".join(
        str(part).lower()
        for part in (
            getattr(latest, "market_observation", ""),
            getattr(latest, "regime_notes", ""),
        )
    )
    confidence = float(getattr(latest, "confidence_level", 0.0) or 0.0)

    positive_terms = ("bullish", "breakout", "strength", "momentum", "recovery")
    negative_terms = ("bearish", "breakdown", "weakness", "selloff", "risk")

    if confidence >= 0.6 and any(term in text for term in positive_terms):
        delta += 0.01
        reasons.append("cognition_positive")
    if confidence >= 0.6 and any(term in text for term in negative_terms):
        delta -= 0.01
        reasons.append("cognition_negative")

    anomalies = getattr(latest, "anomalies", []) or []
    if anomalies:
        delta -= min(0.015, 0.005 * len(anomalies))
        reasons.append("cognition_anomalies")

    return delta, reasons


def _market_observation_hint() -> tuple[float, float, list[str]]:
    try:
        from reflection.reflection_statistics import get_latest_market_observation

        observation = get_latest_market_observation()
    except Exception as exc:
        logger.debug("Market observation advisory unavailable: %s", exc)
        return 0.0, 0.0, []

    if not observation:
        return 0.0, 0.0, []

    confidence_delta = 0.0
    market_delta = 0.0
    reasons: list[str] = []

    market = str(observation.get("market_condition", "UNKNOWN")).upper()
    trend = str(observation.get("trend_strength", "UNKNOWN")).upper()
    volatility = str(observation.get("volatility_regime", "UNKNOWN")).upper()
    breakouts = str(observation.get("breakout_frequency", "UNKNOWN")).upper()
    reversals = str(observation.get("reversal_frequency", "UNKNOWN")).upper()

    if market == "BULLISH":
        confidence_delta += 0.015
        market_delta += 0.015
        reasons.append("market_bullish")
        if trend == "STRONG":
            confidence_delta += 0.01
            market_delta += 0.01
            reasons.append("trend_strong")
    elif market == "BEARISH":
        confidence_delta -= 0.025
        market_delta -= 0.025
        reasons.append("market_bearish")
    elif market in ("MIXED", "RANGING"):
        confidence_delta -= 0.01
        market_delta -= 0.01
        reasons.append(f"market_{market.lower()}")

    if volatility == "HIGH":
        confidence_delta -= 0.01
        reasons.append("vol_high")
    elif volatility == "LOW" and market == "BULLISH":
        confidence_delta += 0.005
        reasons.append("vol_low_bullish")

    if breakouts == "HIGH" and market == "BULLISH":
        confidence_delta += 0.005
        reasons.append("breakouts_high")
    if reversals == "HIGH":
        confidence_delta -= 0.01
        reasons.append("reversals_high")

    return confidence_delta, market_delta, reasons


def get_execution_advisory(symbol: str | None = None) -> dict:
    """
    Convert cognition/observation data into a weak execution advisory.

    This bridge is advisory-only: it returns capped multipliers and never
    places orders, alters risk, changes leverage, or bypasses confidence gates.
    """
    try:
        conf_delta, market_delta, reasons = _market_observation_hint()
        cog_delta, cog_reasons = _latest_cognition_hint()
        conf_delta += cog_delta
        reasons.extend(cog_reasons)

        advisory = ExecutionAdvisory(
            confidence_multiplier=_multiplier_from_delta(conf_delta),
            market_multiplier=_multiplier_from_delta(market_delta),
            reason=", ".join(reasons) if reasons else "neutral",
            source="observation+cognition" if reasons else "none",
        )
        logger.debug("Execution advisory | symbol=%s | %s", symbol or "ALL", advisory.as_dict())
        return advisory.as_dict()
    except Exception as exc:
        logger.warning("Execution advisory failed neutral: %s", exc)
        return ExecutionAdvisory(reason="bridge_error").as_dict()
