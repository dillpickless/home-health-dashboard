"""Cycle phase and pregnancy-risk estimates from period starts + nightly temperature.

Conservative by design: a day is only "low" risk once a sustained temperature
rise has confirmed ovulation in that cycle. Everything unconfirmed is
"possible", and the predicted fertile window is "high". Not contraception.

Temperature shift rule ("3 over 6"): three consecutive nights all above the
highest of the previous six nights (the cover line), the third at least
SHIFT_THRESHOLD_C above it. Ovulation is estimated as the day before the first
high night; the low-risk phase starts the day after the third high night.
"""

from __future__ import annotations

import datetime as dt
from statistics import mean

SHIFT_THRESHOLD_C = 0.2
COVER_WINDOW = 6
MIN_COVER_READINGS = 4
HIGH_NIGHTS = 3
SPERM_SURVIVAL_DAYS = 5
PREDICTION_MARGIN_DAYS = 2
MIN_CYCLES_FOR_PREDICTION = 2
PLAUSIBLE_CYCLE_DAYS = (15, 60)
LONG_HIGH_PHASE_DAYS = 18
FORECAST_DAYS = 35

DAY = dt.timedelta(days=1)


def detect_shift(start: dt.date, end: dt.date, temps: dict[dt.date, float]) -> dict | None:
    """Return the first confirmed temperature shift in [start, end], or None."""
    day = start + COVER_WINDOW * DAY
    while day + (HIGH_NIGHTS - 1) * DAY <= end:
        prior = [temps[d] for d in (day - i * DAY for i in range(1, COVER_WINDOW + 1)) if d in temps]
        highs = [temps.get(day + i * DAY) for i in range(HIGH_NIGHTS)]
        if len(prior) >= MIN_COVER_READINGS and None not in highs:
            cover = max(prior)
            if all(t > cover for t in highs) and highs[-1] >= cover + SHIFT_THRESHOLD_C:
                return {
                    "ovulation": day - DAY,
                    "confirmed_on": day + (HIGH_NIGHTS - 1) * DAY,
                    "cover_line": round(cover, 2),
                }
        day += DAY
    return None


def build_cycles(period_starts: list[dt.date], temps: dict[dt.date, float], today: dt.date) -> list[dict]:
    cycles = []
    starts = sorted(set(period_starts))
    for i, start in enumerate(starts):
        next_start = starts[i + 1] if i + 1 < len(starts) else None
        end = next_start - DAY if next_start else today
        shift = detect_shift(start, end, temps)
        cycles.append(
            {
                "start": start,
                "end": end,
                "complete": next_start is not None,
                "length": (next_start - start).days if next_start else None,
                "ovulation": shift["ovulation"] if shift else None,
                "ovulation_cycle_day": (shift["ovulation"] - start).days + 1 if shift else None,
                "confirmed_on": shift["confirmed_on"] if shift else None,
                "cover_line": shift["cover_line"] if shift else None,
            }
        )
    return cycles


def predict(cycles: list[dict]) -> dict:
    lo, hi = PLAUSIBLE_CYCLE_DAYS
    lengths = [c["length"] for c in cycles if c["complete"] and lo <= c["length"] <= hi]
    ov_days = [c["ovulation_cycle_day"] for c in cycles if c["complete"] and c["ovulation_cycle_day"]]
    current = cycles[-1] if cycles else None
    result = {
        "cycles_logged": len([c for c in cycles if c["complete"]]),
        "cycles_with_ovulation": len(ov_days),
        "avg_cycle_length": round(mean(lengths), 1) if lengths else None,
        "next_period": None,
        "fertile_window": None,
    }
    if current and lengths:
        result["next_period"] = current["start"] + round(mean(lengths)) * DAY
    if current and len(ov_days) >= MIN_CYCLES_FOR_PREDICTION:
        first = min(ov_days) - SPERM_SURVIVAL_DAYS - PREDICTION_MARGIN_DAYS
        last = max(ov_days) + 1
        result["fertile_window"] = (
            current["start"] + (max(first, 1) - 1) * DAY,
            current["start"] + (last - 1) * DAY,
        )
    return result


def classify(
    period_starts: list[dt.date],
    temps: dict[dt.date, float],
    today: dt.date,
    history_days: int = 90,
) -> dict:
    cycles = build_cycles(period_starts, temps, today)
    prediction = predict(cycles)
    first_day = today - history_days * DAY
    last_day = today + FORECAST_DAYS * DAY
    days: dict[dt.date, dict] = {}

    day = first_day
    while day <= last_day:
        days[day] = {"risk": "unknown", "cycle_day": None, "period_start": False,
                     "ovulation_est": False, "predicted": day > today}
        day += DAY

    for idx, cycle in enumerate(cycles):
        is_current = idx == len(cycles) - 1
        # The current cycle runs into the forecast until the predicted next period.
        stop = cycle["end"]
        if is_current:
            stop = (prediction["next_period"] - DAY) if prediction["next_period"] else last_day
            stop = max(stop, today)
        retro_window = None
        if cycle["ovulation"]:
            retro_window = (cycle["ovulation"] - SPERM_SURVIVAL_DAYS * DAY, cycle["ovulation"] + DAY)
        day = cycle["start"]
        while day <= stop:
            if day in days:
                entry = days[day]
                entry["cycle_day"] = (day - cycle["start"]).days + 1
                entry["period_start"] = day == cycle["start"]
                entry["ovulation_est"] = day == cycle["ovulation"]
                if cycle["confirmed_on"] and day > cycle["confirmed_on"]:
                    entry["risk"] = "low"
                elif retro_window and retro_window[0] <= day <= retro_window[1]:
                    entry["risk"] = "high"
                elif (is_current and not cycle["confirmed_on"] and prediction["fertile_window"]
                      and prediction["fertile_window"][0] <= day <= prediction["fertile_window"][1]):
                    entry["risk"] = "high"
                else:
                    entry["risk"] = "possible"
            day += DAY

    if prediction["next_period"]:
        day = prediction["next_period"]
        while day <= last_day:
            if day > today:
                days[day]["risk"] = "possible"
                days[day]["cycle_day"] = (day - prediction["next_period"]).days + 1
            day += DAY

    notes = []
    current = cycles[-1] if cycles else None
    if not cycles:
        notes.append("Log the first day of her next period to start cycle tracking.")
    elif current["confirmed_on"]:
        high_days = (today - current["ovulation"]).days
        if high_days >= LONG_HIGH_PHASE_DAYS:
            notes.append(
                f"Temperature has stayed high for {high_days} days since estimated ovulation. "
                "That can mean pregnancy. Consider taking a test."
            )
    if prediction["cycles_with_ovulation"] < MIN_CYCLES_FOR_PREDICTION:
        need = MIN_CYCLES_FOR_PREDICTION - prediction["cycles_with_ovulation"]
        notes.append(
            f"Fertile-window prediction needs {need} more full cycle(s) with a confirmed "
            "temperature shift. Until then, every unconfirmed day is treated as possibly fertile."
        )

    return {"cycles": cycles, "prediction": prediction, "days": days, "notes": notes}
