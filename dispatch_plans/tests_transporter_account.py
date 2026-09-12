"""
Shape of the transporter-account payload, with SAP stubbed out.

The SQL itself is exercised against the live books by hand; what is worth a test
is the assembly around it, because every mistake there is silent: a missing age
band that disappears instead of reading zero, a total that stops matching the
buckets it is made of, an empty answer that comes back as a payable of nothing
rather than a payable nobody has yet.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from dispatch_plans.transporter_account_reader import HanaTransporterAccountReader


class _Reader(HanaTransporterAccountReader):
    """The reader with its HANA connection replaced by canned rows."""

    def __init__(self, rows):
        # Deliberately not calling super().__init__ -- it would open a socket.
        self.schema = "TEST_SCHEMA"
        self._rows = rows

    def _execute(self, query, params):
        return self._rows


class OutstandingBucketTests(SimpleTestCase):
    def test_missing_bands_read_as_zero_not_absent(self):
        # SAP answers only the bands that have documents. A board drawing a row
        # per band needs all four, and a band that vanished would shift every
        # figure up one row.
        reader = _Reader([(45, 148, 4393297.26, 6600913.48, 2207616.22, 712)])

        outstanding = reader._outstanding()

        self.assertEqual([b["band"] for b in outstanding["buckets"]], [0, 15, 30, 45])
        self.assertEqual(outstanding["buckets"][0]["documents"], 0)
        self.assertEqual(outstanding["buckets"][0]["outstanding"], 0.0)
        self.assertIsNone(outstanding["buckets"][0]["oldest_days"])

    def test_totals_are_the_buckets_added_up(self):
        reader = _Reader(
            [
                (0, 9, 168006.0, 170312.0, 2306.0, 10),
                (15, 4, 134981.0, 138581.0, 3600.0, 29),
                (30, 3, 190632.0, 281202.0, 90570.0, 42),
                (45, 148, 4393297.26, 6600913.48, 2207616.22, 712),
            ]
        )

        outstanding = reader._outstanding()

        self.assertEqual(outstanding["documents"], 164)
        self.assertAlmostEqual(outstanding["outstanding"], 4886916.26, places=2)
        self.assertAlmostEqual(outstanding["billed"], 7191008.48, places=2)
        # Part-paid open invoices: the reason billed and outstanding differ.
        self.assertAlmostEqual(outstanding["paid_against_open"], 2304092.22, places=2)
        self.assertEqual(outstanding["oldest_days"], 712)

    def test_nothing_outstanding_still_answers_a_shape(self):
        outstanding = _Reader([])._outstanding()

        self.assertEqual(outstanding["documents"], 0)
        self.assertEqual(outstanding["outstanding"], 0.0)
        # No document means no age, which is not an age of zero.
        self.assertIsNone(outstanding["oldest_days"])
        self.assertEqual(len(outstanding["buckets"]), 4)


class AwaitingInvoiceTests(SimpleTestCase):
    def test_buckets_zero_fill_and_total(self):
        reader = _Reader(
            [
                (0, 63, 872061.0, 14),
                (15, 21, 161821.94, 26),
                (45, 105, 1159614.11, 569),
            ]
        )

        awaiting = reader._awaiting_invoice()

        self.assertEqual([b["band"] for b in awaiting["buckets"]], [0, 15, 30, 45])
        # The 30 band had no open GRPO -- it reads zero, it does not vanish.
        self.assertEqual(awaiting["buckets"][2]["documents"], 0)
        self.assertEqual(awaiting["documents"], 189)
        self.assertAlmostEqual(awaiting["amount"], 2193497.05, places=2)
        self.assertEqual(awaiting["oldest_days"], 569)

    def test_nothing_awaiting_an_invoice_is_not_a_missing_answer(self):
        awaiting = _Reader([])._awaiting_invoice()

        self.assertEqual(awaiting["documents"], 0)
        self.assertEqual(awaiting["amount"], 0.0)
        self.assertIsNone(awaiting["oldest_days"])
        self.assertEqual(len(awaiting["buckets"]), 4)


class PaymentsTests(SimpleTestCase):
    def test_window_and_latest_date_come_back(self):
        reader = _Reader([(16, 2420510.0, "2026-09-10")])

        payments = reader._payments(30)

        self.assertEqual(payments["window_days"], 30)
        self.assertEqual(payments["payments"], 16)
        self.assertAlmostEqual(payments["paid"], 2420510.0, places=2)
        self.assertEqual(payments["latest_date"], "2026-09-10")

    def test_no_payments_in_the_window_has_no_latest_date(self):
        # SUM over no rows is NULL in HANA, and COUNT is 0 -- a quiet month must
        # not come back as a payment of nothing on a date of nothing.
        payments = _Reader([(0, None, None)])._payments(30)

        self.assertEqual(payments["payments"], 0)
        self.assertEqual(payments["paid"], 0.0)
        self.assertIsNone(payments["latest_date"])


class VendorBreakdownTests(SimpleTestCase):
    def test_rows_map_heaviest_first_as_sap_ordered_them(self):
        reader = _Reader(
            [
                ("VENDA000055", "OM LOGISTICS LTD", 21, 2861703.0, 552),
                ("VENDA000636", "DELHI PUNJAB TRANSPORT CO", 12, 785675.0, 285),
            ]
        )

        vendors = reader._by_vendor()

        self.assertEqual(vendors[0]["card_code"], "VENDA000055")
        self.assertEqual(vendors[0]["documents"], 21)
        self.assertAlmostEqual(vendors[0]["outstanding"], 2861703.0, places=2)
        self.assertEqual(vendors[1]["oldest_days"], 285)


class PaymentWindowClampTests(SimpleTestCase):
    def test_get_account_refuses_a_zero_or_negative_window(self):
        reader = _Reader([])

        with patch.object(_Reader, "_payments", return_value={}) as payments:
            reader.get_account(payment_days=0)

        # A window of zero days would read as "nothing paid" rather than as a
        # question nobody asked.
        payments.assert_called_once_with(30)
