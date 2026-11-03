"""Tests for temperature.py. Run: .venv/bin/python -m unittest"""

import datetime as dt
import unittest

import temperature

D0 = dt.date(2026, 1, 1)
DAY = dt.timedelta(days=1)


class Calibrate(unittest.TestCase):
    def test_recovers_baseline(self):
        deviations = {D0 + i * DAY: dev for i, dev in enumerate([0.1, -0.2, 0.3, 0.0])}
        readings = {day: 36.5 + dev for day, dev in deviations.items()}
        cal = temperature.calibrate(readings, deviations)
        self.assertAlmostEqual(cal["baseline_c"], 36.5)
        self.assertAlmostEqual(cal["typical_error_c"], 0.0)
        self.assertTrue(cal["ready"])

    def test_one_bad_reading_barely_moves_it(self):
        deviations = {D0 + i * DAY: 0.0 for i in range(5)}
        readings = {D0 + i * DAY: t for i, t in enumerate([36.5, 36.6, 36.4, 36.5, 38.5])}
        self.assertAlmostEqual(temperature.calibrate(readings, deviations)["baseline_c"], 36.5)

    def test_needs_matching_oura_night(self):
        cal = temperature.calibrate({D0: 36.5, D0 + DAY: 36.6}, {D0: 0.1})
        self.assertEqual((cal["matched"], cal["unmatched"]), (1, 1))
        self.assertFalse(cal["ready"])

    def test_no_readings(self):
        cal = temperature.calibrate({}, {D0: 0.1})
        self.assertIsNone(cal["baseline_c"])
        self.assertFalse(cal["ready"])

    def test_estimate(self):
        self.assertAlmostEqual(temperature.estimate(36.5, 0.3), 36.8)
        self.assertIsNone(temperature.estimate(None, 0.3))
        self.assertIsNone(temperature.estimate(36.5, None))


if __name__ == "__main__":
    unittest.main()
