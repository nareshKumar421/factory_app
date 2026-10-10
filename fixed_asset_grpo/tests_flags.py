"""The switches and the live-SAP guard, without a database.

* each flag follows ``DEBUG`` (or False) until it is set, and is read on every call;
* a cut-over that is not an ISO date is a configuration error, not a guess;
* the guard reads the company's own Service Layer database and HANA schema
  and names the switch to turn on; ALLOW_LIVE lets a live company through.

    manage.py test fixed_asset_grpo.tests_flags --settings=config.sqlite_test_settings
"""

from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from . import fa_test_support as fx
from . import flags

GUARD_CONTEXT = "sap_purchasing.live_guard.CompanyContext"


class FlagTests(SimpleTestCase):
    def test_enabled_and_simulate_follow_debug_until_set(self):
        for name in (flags.ENABLED, flags.SIMULATE):
            if hasattr(settings, name):
                self.skipTest(f"{name} is set in settings; the default cannot be observed")
        with override_settings(DEBUG=True):
            self.assertTrue(flags.enabled())
            self.assertTrue(flags.simulate())
        with override_settings(DEBUG=False):
            self.assertFalse(flags.enabled())
            self.assertFalse(flags.simulate())

    def test_allow_live_is_off_unless_set(self):
        if hasattr(settings, flags.ALLOW_LIVE):
            self.skipTest("FIXED_ASSET_GRPO_ALLOW_LIVE is set in settings")
        with override_settings(DEBUG=True):
            self.assertFalse(flags.allow_live())

    def test_an_explicit_flag_beats_debug(self):
        with override_settings(DEBUG=True, FIXED_ASSET_GRPO_ENABLED=False,
                               FIXED_ASSET_GRPO_SIMULATE=False):
            self.assertFalse(flags.enabled())
            self.assertFalse(flags.simulate())

    def test_cutover(self):
        with override_settings(FIXED_ASSET_GRPO_CUTOVER=""):
            self.assertIsNone(flags.cutover())
        with override_settings(FIXED_ASSET_GRPO_CUTOVER="2026-11-01"):
            self.assertEqual(flags.cutover(), date(2026, 11, 1))
        with override_settings(FIXED_ASSET_GRPO_CUTOVER="1/11/2026"):
            with self.assertRaises(ImproperlyConfigured):
                flags.cutover()

    @override_settings(FIXED_ASSET_WAREHOUSES="BH-FA, DL-FA,,BH-FA")
    def test_warehouses_are_cleaned(self):
        self.assertEqual(flags.warehouses(), ["BH-FA", "DL-FA"])


class LiveGuardTests(SimpleTestCase):
    @override_settings(FIXED_ASSET_GRPO_ALLOW_LIVE=False)
    def test_a_live_company_is_refused_by_name(self):
        with patch(GUARD_CONTEXT, fx.company_context("JIVO_OIL")):
            with self.assertRaises(ValueError) as caught:
                flags.require_live_allowed("JIVO_OIL")
        self.assertIn("FIXED_ASSET_GRPO_ALLOW_LIVE", str(caught.exception))

    @override_settings(FIXED_ASSET_GRPO_ALLOW_LIVE=False)
    def test_a_test_company_passes(self):
        with patch(GUARD_CONTEXT, fx.company_context("TEST_JIVO_OIL")):
            flags.require_live_allowed("JIVO_OIL")
            self.assertTrue(flags.is_test_db("JIVO_OIL"))

    @override_settings(FIXED_ASSET_GRPO_ALLOW_LIVE=True)
    def test_allow_live_lets_a_live_company_through(self):
        with patch(GUARD_CONTEXT, fx.company_context("JIVO_OIL")):
            flags.require_live_allowed("JIVO_OIL")

    def test_an_unknown_company_is_not_known_for_the_badge(self):
        with patch(GUARD_CONTEXT, side_effect=Exception("unknown company")):
            self.assertIsNone(flags.is_test_db("NOPE"))
