"""The live-SAP guard reads the databases the writers use, and fails closed.

``CompanyContext`` is patched where the guard looks it up
(``sap_purchasing.live_guard.CompanyContext``): the registry is an import-time
snapshot of ``settings.COMPANY_DB``, so overriding the setting would test
nothing.

Run: ``manage.py test sap_purchasing.tests_live_guard --settings=config.sqlite_test_settings``
"""

from unittest import mock

from django.test import SimpleTestCase

from sap_client.exceptions import SAPValidationError
from sap_purchasing import live_guard


def _ctx(sl_db, schema):
    ctx = mock.Mock()
    ctx.service_layer = {"company_db": sl_db}
    ctx.hana = {"schema": schema}
    return ctx


class LiveGuardTests(SimpleTestCase):
    def _patch(self, sl_db, schema):
        return mock.patch("sap_purchasing.live_guard.CompanyContext",
                          return_value=_ctx(sl_db, schema))

    def test_both_test_databases_pass(self):
        with self._patch("TEST_JIVO_OIL_HANADB", "TEST_JIVO_OIL_HANADB"):
            self.assertTrue(live_guard.is_test_target("JIVO_OIL"))
            self.assertIsNone(live_guard.require_test_target(
                "JIVO_OIL", allow_live=False, switch_name="CASH_SAP_ALLOW_LIVE"))

    def test_prefix_is_case_insensitive(self):
        with self._patch("test_jivo_oil", "Test_Jivo_Oil"):
            self.assertTrue(live_guard.is_test_target("JIVO_OIL"))

    def test_live_service_layer_is_refused(self):
        with self._patch("JIVO_OIL_HANADB", "TEST_JIVO_OIL_HANADB"):
            self.assertFalse(live_guard.is_test_target("JIVO_OIL"))
            with self.assertRaises(ValueError) as caught:
                live_guard.require_test_target(
                    "JIVO_OIL", allow_live=False, switch_name="CASH_SAP_ALLOW_LIVE")
        self.assertIn("CASH_SAP_ALLOW_LIVE", str(caught.exception))
        self.assertIn("JIVO_OIL_HANADB", str(caught.exception))

    def test_live_hana_schema_is_refused(self):
        with self._patch("TEST_JIVO_OIL_HANADB", "JIVO_OIL_HANADB"):
            with self.assertRaises(ValueError) as caught:
                live_guard.require_test_target(
                    "JIVO_OIL", allow_live=False, switch_name="FIXED_ASSET_GRPO_ALLOW_LIVE")
        self.assertIn("FIXED_ASSET_GRPO_ALLOW_LIVE", str(caught.exception))

    def test_allow_live_lets_a_live_company_through(self):
        with self._patch("JIVO_OIL_HANADB", "JIVO_OIL_HANADB"):
            self.assertIsNone(live_guard.require_test_target(
                "JIVO_OIL", allow_live=True, switch_name="CASH_SAP_ALLOW_LIVE"))

    def test_blank_database_counts_as_live(self):
        with self._patch("", "TEST_X"):
            self.assertFalse(live_guard.is_test_target("JIVO_OIL"))
            with self.assertRaises(ValueError):
                live_guard.require_test_target("JIVO_OIL", allow_live=False, switch_name="S")

    def test_target_databases_reads_the_context(self):
        with self._patch("TEST_A", "TEST_B") as ctx:
            self.assertEqual(live_guard.target_databases("JIVO_MART"), ("TEST_A", "TEST_B"))
        ctx.assert_called_with("JIVO_MART")

    def test_unknown_company_raises_from_the_registry(self):
        with mock.patch("sap_purchasing.live_guard.CompanyContext",
                        side_effect=SAPValidationError("Invalid company code: NOPE")):
            with self.assertRaises(SAPValidationError):
                live_guard.require_test_target("NOPE", allow_live=False, switch_name="S")
