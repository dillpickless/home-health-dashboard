"""Tests for cycle.py with synthetic temperature curves. Run: .venv/bin/python -m unittest"""

import datetime as dt
import unittest

import cycle

D0 = dt.date(2026, 1, 1)
DAY = dt.timedelta(days=1)


def biphasic(start: dt.date, length: int, ovulation_day: int, low=-0.1, high=0.3) -> dict:
    """Temps for one cycle: low until ovulation_day (1-based), high after it."""
    return {start + i * DAY: (high if i + 1 > ovulation_day else low + 0.02 * (i % 3))
            for i in range(length)}


class DetectShift(unittest.TestCase):
    def test_finds_shift(self):
        temps = biphasic(D0, 28, ovulation_day=14)
        shift = cycle.detect_shift(D0, D0 + 27 * DAY, temps)
        self.assertEqual(shift["ovulation"], D0 + 13 * DAY)  # cycle day 14
        self.assertEqual(shift["confirmed_on"], D0 + 16 * DAY)

    def test_flat_temps_never_confirm(self):
        temps = {D0 + i * DAY: 0.0 for i in range(28)}
        self.assertIsNone(cycle.detect_shift(D0, D0 + 27 * DAY, temps))

    def test_small_rise_does_not_confirm(self):
        temps = biphasic(D0, 28, ovulation_day=14, low=0.0, high=0.15)
        self.assertIsNone(cycle.detect_shift(D0, D0 + 27 * DAY, temps))

    def test_missing_night_blocks_confirmation(self):
        temps = biphasic(D0, 28, ovulation_day=14)
        del temps[D0 + 15 * DAY]  # second high night missing
        # Later high nights become the new cover line, so the cycle never
        # confirms: the safe outcome when data is missing.
        self.assertIsNone(cycle.detect_shift(D0, D0 + 27 * DAY, temps))


class Classify(unittest.TestCase):
    def test_no_periods_everything_unknown(self):
        result = cycle.classify([], {}, today=D0)
        self.assertTrue(all(d["risk"] == "unknown" for d in result["days"].values()))
        self.assertTrue(result["notes"])

    def test_first_cycle_low_only_after_confirmation(self):
        today = D0 + 20 * DAY
        result = cycle.classify([D0], biphasic(D0, 21, ovulation_day=14), today)
        days = result["days"]
        self.assertEqual(days[D0 + 16 * DAY]["risk"], "possible")  # confirmation day itself
        self.assertEqual(days[D0 + 17 * DAY]["risk"], "low")       # day after
        self.assertEqual(days[D0 + 8 * DAY]["risk"], "high")       # inside retro window
        self.assertEqual(days[D0 + 2 * DAY]["risk"], "possible")   # early cycle, never "low"

    def test_unconfirmed_current_cycle_is_never_low(self):
        today = D0 + 10 * DAY
        result = cycle.classify([D0], biphasic(D0, 11, ovulation_day=30), today)
        risks = {d["risk"] for k, d in result["days"].items() if k >= D0}
        self.assertNotIn("low", risks)

    def test_prediction_after_two_cycles(self):
        starts = [D0, D0 + 28 * DAY, D0 + 56 * DAY]
        temps = {}
        for s in starts[:2]:
            temps |= biphasic(s, 28, ovulation_day=14)
        today = starts[2] + 2 * DAY
        result = cycle.classify(starts, temps, today)
        pred = result["prediction"]
        self.assertEqual(pred["next_period"], starts[2] + 28 * DAY)
        window = pred["fertile_window"]
        self.assertEqual(window[0], starts[2] + (14 - 5 - 2 - 1) * DAY)
        self.assertEqual(window[1], starts[2] + 14 * DAY)
        self.assertEqual(result["days"][starts[2] + 10 * DAY]["risk"], "high")
        self.assertEqual(result["days"][starts[2] + 3 * DAY]["risk"], "possible")

    def test_long_high_phase_flags_possible_pregnancy(self):
        temps = biphasic(D0, 40, ovulation_day=14)
        result = cycle.classify([D0], temps, today=D0 + 39 * DAY)
        self.assertTrue(any("pregnancy" in n for n in result["notes"]))


if __name__ == "__main__":
    unittest.main()
