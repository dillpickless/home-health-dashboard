"""Estimate actual body temperature from Oura's deviations plus a few thermometer readings.

Oura only reports each night's temperature as a deviation from her personal
baseline, never the baseline itself. Each thermometer reading taken on waking
gives one sample of that baseline: reading minus that night's deviation. The
median of those samples is her estimated baseline, and from then on any night's
estimated temperature is baseline + deviation.

The ring measures finger skin and a thermometer measures the mouth, so this is
an approximation. `typical_error_c` reports how much the samples disagree.
"""

from __future__ import annotations

import datetime as dt
from statistics import median

MIN_READINGS = 3
# Plausible on-waking oral readings, °C (95.0–104.0 °F).
VALID_RANGE_C = (35.0, 40.0)


def calibrate(readings: dict[dt.date, float], deviations: dict[dt.date, float]) -> dict:
    """readings and deviations are in °C, keyed by the day she woke up."""
    samples = {day: temp - deviations[day] for day, temp in readings.items() if day in deviations}
    if not samples:
        return {"baseline_c": None, "typical_error_c": None, "matched": 0,
                "unmatched": len(readings), "ready": False}
    baseline = median(samples.values())
    return {
        "baseline_c": round(baseline, 3),
        # Median absolute deviation: robust to one odd reading.
        "typical_error_c": round(median(abs(s - baseline) for s in samples.values()), 3),
        "matched": len(samples),
        "unmatched": len(readings) - len(samples),
        "ready": len(samples) >= MIN_READINGS,
    }


def estimate(baseline_c: float | None, deviation_c: float | None) -> float | None:
    if baseline_c is None or deviation_c is None:
        return None
    return round(baseline_c + deviation_c, 3)
