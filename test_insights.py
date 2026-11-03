"""Tests for insights.py. Run: .venv/bin/python -m unittest"""

import datetime as dt
import unittest

import insights

TODAY = dt.date(2026, 3, 1)


def base(ready=True):
    def b(median):
        return {"median": median, "nights": 20 if ready else 3, "ready": ready}
    return {"hrv": b(50), "rhr": b(55), "sleep_hours": b(7.5)}


def latest(**kw):
    row = {"readiness": 80, "sleep_hours": 7.5, "hrv": 50, "rhr": 55, "temp": 0.0}
    row.update(kw)
    return row


class Phase(unittest.TestCase):
    def phase(self, cycle_day, confirmed=None, next_period=None):
        cycle = {"start": TODAY, "confirmed_on": confirmed}
        return insights.cycle_phase({"cycle_day": cycle_day}, cycle, next_period, TODAY)

    def test_phases(self):
        self.assertEqual(self.phase(3), "period")
        self.assertEqual(self.phase(10), "follicular")
        self.assertEqual(self.phase(20, confirmed=TODAY - dt.timedelta(days=2)), "luteal")
        self.assertEqual(self.phase(26, confirmed=TODAY - dt.timedelta(days=8),
                                    next_period=TODAY + dt.timedelta(days=2)), "pre_period")

    def test_no_cycle(self):
        self.assertIsNone(insights.cycle_phase({"cycle_day": None}, None, None, TODAY))


class Strain(unittest.TestCase):
    def test_fires_before_ovulation(self):
        self.assertIsNotNone(insights.strain_signal(latest(rhr=59, hrv=38), base(), "follicular"))

    def test_same_change_after_ovulation_is_normal(self):
        self.assertIsNone(insights.strain_signal(latest(rhr=59, hrv=38), base(), "luteal"))

    def test_big_change_after_ovulation_still_fires(self):
        signal = insights.strain_signal(latest(rhr=61, hrv=33), base(), "luteal")
        self.assertIn("post-ovulation", signal["text"])

    def test_needs_baseline(self):
        self.assertIsNone(insights.strain_signal(latest(rhr=70, hrv=20), base(ready=False), None))


class Summary(unittest.TestCase):
    def text(self, row, phase=None, b=None):
        return " ".join(insights.daily_summary(row, b or base(), phase, 20))

    def test_post_ovulation_changes_explained(self):
        text = self.text(latest(rhr=58, hrv=42, temp=0.3), phase="luteal")
        self.assertIn("typical after ovulation", text)
        self.assertIn("as expected after ovulation", text)

    def test_only_names_what_changed(self):
        text = self.text(latest(rhr=54, hrv=40), phase="luteal")
        self.assertIn("Her HRV is a little lower", text)
        self.assertNotIn("heart rate is a little higher", text)

    def test_same_numbers_without_cycle_context(self):
        text = self.text(latest(rhr=58, hrv=42))
        self.assertIn("working harder", text)

    def test_sleep_difference(self):
        self.assertIn("1h 00m less than usual", self.text(latest(sleep_hours=6.5)))

    def test_learning_note(self):
        self.assertIn("Still learning", self.text(latest(), b=base(ready=False)))

    def test_no_data(self):
        self.assertIn("No data", insights.daily_summary(None, base(), None, None)[0])


if __name__ == "__main__":
    unittest.main()
