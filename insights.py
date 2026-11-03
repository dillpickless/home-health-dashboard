"""Plain-English daily summary that reads her numbers in light of her cycle phase.

After ovulation it is normal for temperature and resting heart rate to rise and
HRV to dip. A generic app reads that as "worse recovery"; here it is explained
as expected, and the run-down warning needs a bigger change before it fires.
All text is rule-based so it is predictable and testable.
"""

from __future__ import annotations

import datetime as dt

PERIOD_DAYS = 5
PRE_PERIOD_DAYS = 3

PHASES = {
    "period": ("Period week",
               "Energy and sleep can dip a little. Gentle movement and extra rest are normal."),
    "follicular": ("Before ovulation",
                   "Usually the stretch where energy, recovery and HRV feel best."),
    "luteal": ("After ovulation",
               "Temperature and resting heart rate run a bit higher and HRV a bit lower. That's normal."),
    "pre_period": ("Days before her period",
                   "Sleep can get lighter and resting heart rate is often at its highest. Normal, not a warning sign."),
}


def cycle_phase(today_entry: dict, current_cycle: dict | None, next_period: dt.date | None,
                today: dt.date) -> str | None:
    if not current_cycle or not today_entry.get("cycle_day"):
        return None
    if today_entry["cycle_day"] <= PERIOD_DAYS:
        return "period"
    confirmed = current_cycle.get("confirmed_on")
    if confirmed and today > confirmed:
        if next_period and (next_period - today).days <= PRE_PERIOD_DAYS:
            return "pre_period"
        return "luteal"
    return "follicular"


def strain_signal(latest: dict | None, base: dict, phase: str | None) -> dict | None:
    """Flag the 'getting sick or run down' pattern: RHR up and HRV down together.

    After ovulation both shift naturally, so the bar is higher then.
    """
    if not latest or not (base["rhr"]["ready"] and base["hrv"]["ready"]):
        return None
    if latest["rhr"] is None or latest["hrv"] is None or not base["hrv"]["median"]:
        return None
    post_ovulation = phase in ("luteal", "pre_period")
    rhr_limit, hrv_limit = (5, 0.7) if post_ovulation else (3, 0.8)
    rhr_up = latest["rhr"] - base["rhr"]["median"]
    hrv_ratio = latest["hrv"] / base["hrv"]["median"]
    if rhr_up >= rhr_limit and hrv_ratio <= hrv_limit:
        context = (" That's more than the usual post-ovulation change." if post_ovulation else "")
        return {
            "level": "warning",
            "text": f"Resting heart rate is {rhr_up:.0f} bpm above her normal and HRV is "
                    f"{(1 - hrv_ratio) * 100:.0f}% below.{context} Her body may be fighting "
                    "something or under strain. Take it easy today.",
        }
    return None


def _minutes(hours: float) -> str:
    total = round(abs(hours) * 60)
    h, m = divmod(total, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def daily_summary(latest: dict | None, base: dict, phase: str | None,
                  cycle_day: int | None) -> list[str]:
    if not latest:
        return ["No data from last night yet. Once she sleeps with the ring and opens the Oura app, "
                "her summary shows up here."]
    lines = []
    post_ovulation = phase in ("luteal", "pre_period")

    readiness = latest.get("readiness")
    if readiness is not None:
        if readiness >= 85:
            lines.append("Her body is well recovered. A good day for something demanding.")
        elif readiness >= 70:
            lines.append("Recovery looks solid. A normal day.")
        elif readiness >= 60:
            lines.append("Recovery is a bit low. Keep things lighter if she can.")
        else:
            lines.append("Her body is asking for rest today.")

    hours = latest.get("sleep_hours")
    if hours is not None:
        normal = base["sleep_hours"]
        if normal["ready"]:
            diff = hours - normal["median"]
            if abs(diff) < 0.25:
                lines.append(f"She slept {_minutes(hours)}, about her usual.")
            else:
                more = "more" if diff > 0 else "less"
                lines.append(f"She slept {_minutes(hours)}, {_minutes(diff)} {more} than usual.")
        else:
            lines.append(f"She slept {_minutes(hours)}.")

    rhr, hrv = latest.get("rhr"), latest.get("hrv")
    if base["rhr"]["ready"] and base["hrv"]["ready"] and rhr is not None and hrv is not None:
        rhr_up = rhr - base["rhr"]["median"] >= 2
        hrv_down = hrv < base["hrv"]["median"] * 0.9
        what = ("Her resting heart rate is a little higher and HRV a little lower than usual"
                if rhr_up and hrv_down else
                "Her resting heart rate is a little higher than usual" if rhr_up else
                "Her HRV is a little lower than usual")
        if (rhr_up or hrv_down) and post_ovulation:
            lines.append(f"{what}. That's typical after ovulation, so it isn't a red flag by itself.")
        elif rhr_up and hrv_down:
            lines.append("Her heart is working harder than usual overnight. Stress, a late meal, alcohol "
                         "or a cold coming on can all do this.")
        elif rhr_up or hrv_down:
            lines.append(f"{what}. A short night, stress, a late meal or alcohol are common reasons.")
        elif hrv >= base["hrv"]["median"] * 1.1:
            lines.append("Her heart rate and HRV look great. Her nervous system recovered well.")

    temp = latest.get("temp")
    if temp is not None:
        if post_ovulation and temp >= 0.1:
            lines.append("Her temperature is up, as expected after ovulation.")
        elif phase == "follicular" and temp >= 0.25:
            lines.append("Her temperature is higher than usual. If it stays up for 3 nights in a row, "
                         "that usually means she has ovulated.")
        elif phase is None and temp >= 0.5:
            lines.append("Her temperature is noticeably higher than usual. Keep an eye on how she feels.")

    if phase:
        label, explainer = PHASES[phase]
        lines.append(f"Cycle day {cycle_day}: {label.lower()}. {explainer}")
    if not base["hrv"]["ready"]:
        lines.append(f"Still learning her normal ({base['hrv']['nights']}/14 nights). "
                     "Comparisons get smarter after two weeks.")
    return lines
