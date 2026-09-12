"""
Shape of the SAP freight read, with HANA stubbed out.

The SQL is exercised against the live books by hand; what is worth a test is the
assembly, because every mistake there produces a plausible rate rather than an
error: a haulier's spend lost from the total, an empty window reported as
freight of nothing, a vendor row with no name rendering blank on a wall.
"""

from django.test import SimpleTestCase

from dispatch_plans.freight_rate_service import FreightRateService


class _Service(FreightRateService):
    """The service with its HANA connection replaced by canned rows."""

    def __init__(self, rows):
        # Deliberately not calling super().__init__ -- it would open a socket.
        self.company_code = "JIVO_OIL"
        self.schema = "TEST_SCHEMA"
        self._rows = rows

    def _execute(self, query, params):
        return self._rows


class FreightTotalTests(SimpleTestCase):
    def test_the_total_is_every_hauliers_spend_added(self):
        rate = _Service(
            [
                ("VENDA000055", "OM LOGISTICS LTD", 40, 920_584.0),
                ("VENDA001661", "PICK & SHIP LOGISTICS", 14, 666_757.0),
            ]
        ).get_rate("2026-09-01", "2026-09-12")

        self.assertAlmostEqual(rate["amount"], 1_587_341.0, places=2)
        self.assertEqual(rate["documents"], 54)
        self.assertEqual(rate["vendors"], 2)

    def test_hauliers_come_back_dearest_first_as_sap_ordered_them(self):
        rate = _Service(
            [
                ("VENDA000055", "OM LOGISTICS LTD", 40, 920_584.0),
                ("VENDA001661", "PICK & SHIP LOGISTICS", 14, 666_757.0),
            ]
        ).get_rate("2026-09-01", "2026-09-12")

        names = [row["transporter_name"] for row in rate["by_transporter"]]
        self.assertEqual(names, ["OM LOGISTICS LTD", "PICK & SHIP LOGISTICS"])

    def test_a_window_with_no_freight_posted_is_a_shape_not_a_rate(self):
        # Nothing received yet this month. The tile divides by litres and must
        # be able to tell "nothing posted" from "freight is free".
        rate = _Service([]).get_rate("2026-09-01", "2026-09-12")

        self.assertEqual(rate["amount"], 0.0)
        self.assertEqual(rate["documents"], 0)
        self.assertEqual(rate["by_transporter"], [])

    def test_a_vendor_with_no_name_falls_back_to_its_card_code(self):
        # A blank row on a wall board is unreadable; the code at least
        # identifies which account to go and look at.
        rate = _Service([("VENDA000099", "   ", 1, 5_000.0)]).get_rate(
            "2026-09-01", "2026-09-12"
        )

        self.assertEqual(rate["by_transporter"][0]["transporter_name"], "VENDA000099")

    def test_nulls_from_sap_do_not_become_freight(self):
        # SUM over no matching lines is NULL in HANA, not 0.
        rate = _Service([("VENDA000055", "OM LOGISTICS LTD", None, None)]).get_rate(
            "2026-09-01", "2026-09-12"
        )

        self.assertEqual(rate["amount"], 0.0)
        self.assertEqual(rate["documents"], 0)


class ScopingTests(SimpleTestCase):
    def test_the_query_is_bound_to_the_transporter_group_and_freight_account(self):
        # The account is what keeps INBOUND haulage out of a rate divided by
        # dispatched litres, so losing it would silently inflate the numerator.
        captured = {}

        class _Capturing(_Service):
            def _execute(self, query, params):
                captured["query"] = query
                captured["params"] = params
                return []

        _Capturing([]).get_rate("2026-09-01", "2026-09-12")

        self.assertEqual(
            captured["params"], ["TRANSPORTER", "5670001", "2026-09-01", "2026-09-12"]
        )
        self.assertIn('D."DocType" = \'S\'', captured["query"])
        self.assertIn('IFNULL(D."CANCELED", \'N\') = \'N\'', captured["query"])
