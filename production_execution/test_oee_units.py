"""Unit tests for the shared per-run OEE math (bottles/hr rated speed)."""
from django.test import SimpleTestCase

from production_execution.services.report_service import compute_run_oee


class ComputeRunOeeTests(SimpleTestCase):
    def test_bottles_per_hour_units(self):
        # 720 min available, 60 min breakdown -> 11 operating hours.
        # 24 bottles/case, rated 2400 bottles/hr, 1045 cases produced.
        availability, performance, quality, oee = compute_run_oee(
            total_production=1045, breakdown_minutes=60,
            rated_speed=2400, pieces_per_case=24, rejected_qty=45,
        )
        self.assertAlmostEqual(availability, 660 / 720 * 100, places=4)
        self.assertAlmostEqual(performance, (1045 * 24 / 11) / 2400 * 100, places=4)
        self.assertAlmostEqual(quality, (1045 - 45) / 1045 * 100, places=4)
        self.assertAlmostEqual(
            oee, availability * performance * quality / 10000, places=4)

    def test_missing_factor_treats_case_as_one_bottle(self):
        _, performance, _, _ = compute_run_oee(
            total_production=1100, breakdown_minutes=0,
            rated_speed=100, pieces_per_case=None, rejected_qty=0,
        )
        self.assertAlmostEqual(performance, (1100 / 12) / 100 * 100, places=4)

    def test_performance_capped_at_100(self):
        _, performance, _, _ = compute_run_oee(
            total_production=10000, breakdown_minutes=0,
            rated_speed=100, pieces_per_case=1, rejected_qty=0,
        )
        self.assertEqual(performance, 100)

    def test_degenerate_inputs_are_safe(self):
        availability, performance, quality, oee = compute_run_oee(
            total_production=0, breakdown_minutes=720,
            rated_speed=None, pieces_per_case=None, rejected_qty=0,
        )
        self.assertEqual(availability, 0.0)
        self.assertEqual(performance, 0)
        self.assertEqual(quality, 100)
        self.assertEqual(oee, 0.0)

    def test_no_rated_speed_gives_zero_performance(self):
        _, performance, _, _ = compute_run_oee(
            total_production=500, breakdown_minutes=0,
            rated_speed=0, pieces_per_case=24, rejected_qty=0,
        )
        self.assertEqual(performance, 0)
