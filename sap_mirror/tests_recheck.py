"""The re-check: bills handed out from the copy while HANA was down, compared with
SAP once it answers, and anything cancelled, credited or changed told to the
people who started work on it.

Serving runs the real dispatch reader with HANA failing; the copy and the
re-check run against the fake SAP of ``tests_bills``.
"""

from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

from dispatch_plans.hana_reader import HanaDispatchBillReader
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection
from short_dispatch.models import ShortDispatch

from . import tests_bills as fixtures
from .models import ServedBill, ServedBillOutcome

LATER = timezone.make_aware(datetime(2026, 9, 30, 11, 0))


class RecheckTestCase(fixtures.BillCopyTestCase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        notify = patch("notifications.services.NotificationService.send_notification_to_user")
        self.sent = notify.start()
        self.addCleanup(notify.stop)
        self.manager = get_user_model().objects.create(
            email="manager@example.com", full_name="Plant Manager", is_superuser=True
        )

    def serve_while_down(self, number="626090001"):
        with patch.object(HanaConnection, "connect", side_effect=fixtures.HANA_DOWN):
            return HanaDispatchBillReader(CompanyContext("JIVO_OIL")).get_bill_by_number(number)

    def sap_is_back(self):
        return self.take_copy(LATER)

    def served(self):
        return ServedBill.objects.get(doc_entry=9001)

    def change_in_sap(self, version="2026-09-30|104500", lines=None, **header):
        bill, old_lines, pickable, _ = self.sap.bills[9001]
        self.sap.bills[9001] = ({**bill, **header}, lines or old_lines, pickable, version)


class RecordingTests(RecheckTestCase):
    def test_a_bill_taken_from_the_copy_is_recorded_once(self):
        self.serve_while_down()
        self.serve_while_down()

        served = ServedBill.objects.get()
        self.assertEqual((served.doc_entry, served.outcome), (9001, ServedBillOutcome.PENDING))
        self.assertEqual(served.copy_as_of, fixtures.NOW)

    def test_a_list_a_page_merely_shows_is_not_recorded(self):
        from datetime import date

        with patch.object(HanaConnection, "connect", side_effect=fixtures.HANA_DOWN):
            HanaDispatchBillReader(CompanyContext("JIVO_OIL")).list_bills(
                {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
            )

        self.assertFalse(ServedBill.objects.exists())


class RecheckTests(RecheckTestCase):
    def test_an_unchanged_bill_bothers_nobody(self):
        self.serve_while_down()

        self.sap_is_back()

        self.assertEqual(self.served().outcome, ServedBillOutcome.UNCHANGED)
        self.sent.assert_not_called()

    def test_the_dispatch_stamp_alone_is_not_a_change(self):
        self.serve_while_down()
        self.change_in_sap(
            sap_dispatch_date="2026-09-30", sap_bilty_no="B-77", sap_vehicle_no="HR55AB1234",
            sap_eway_bill="331009876543",
        )

        self.sap_is_back()

        self.assertEqual(self.served().outcome, ServedBillOutcome.UNCHANGED)
        self.sent.assert_not_called()

    def test_a_cancelled_bill_is_told_to_whoever_started_on_it(self):
        clerk = get_user_model().objects.create(email="clerk@example.com", full_name="Store Clerk")
        self.serve_while_down()
        short = ShortDispatch.objects.create(
            company=self.oil, entry_no="SD-20260930-0001", sap_invoice_doc_entry=9001,
            warehouse_code="BH-PC", created_by=clerk,
        )
        del self.sap.bills[9001]

        self.sap_is_back()

        served = self.served()
        self.assertEqual(served.outcome, ServedBillOutcome.CANCELLED)
        self.assertEqual(served.linked[0]["what"], "Short dispatch SD-20260930-0001")
        told = {call.kwargs["user"].pk for call in self.sent.call_args_list}
        self.assertEqual(told, {clerk.pk, self.manager.pk})
        call = self.sent.call_args_list[0].kwargs
        self.assertEqual(call["title"], "Bill 626090001 was cancelled in SAP")
        self.assertIn("handed out from the SAP copy of 30 Sep 10:00", call["body"])
        self.assertIn("Short dispatch SD-20260930-0001", call["body"])
        self.assertEqual(call["click_action_url"], f"/warehouse/short-dispatch/{short.pk}")

    def test_a_quantity_change_is_spelled_out(self):
        self.serve_while_down()
        self.change_in_sap(lines=[fixtures.line(quantity=80.0)])

        self.sap_is_back()

        served = self.served()
        self.assertEqual(served.outcome, ServedBillOutcome.CHANGED)
        self.assertEqual(served.differences, ["Line 1 (FG0000151): quantity 100 → 80"])
        self.assertIn("quantity 100 → 80", self.sent.call_args.kwargs["body"])

    def test_a_new_ship_to_is_a_change(self):
        self.serve_while_down()
        self.change_in_sap(ship_to_code="DEL")

        self.sap_is_back()

        self.assertEqual(self.served().differences, ["Ship-to: GGN → DEL"])

    def test_a_credit_note_is_flagged(self):
        self.serve_while_down()
        self.sap.credited = {9001}

        self.sap_is_back()

        self.assertEqual(self.served().outcome, ServedBillOutcome.CREDITED)
        self.assertTrue(self.sent.called)

    def test_a_bill_older_than_the_copy_is_checked_in_sap_itself(self):
        self.serve_while_down()
        # Aged out of the 30 days: the copy drops it, SAP still has it, unchanged.
        bill, lines, pickable, version = self.sap.bills[9001]
        self.sap.bills[9001] = ({**bill, "create_date": "2026-08-01"}, lines, pickable, version)

        self.sap_is_back()

        self.assertEqual(self.served().outcome, ServedBillOutcome.UNCHANGED)

    def test_a_check_that_fails_is_tried_again_next_run(self):
        self.serve_while_down()
        del self.sap.bills[9001]
        with patch.object(fixtures.FakeReader, "list_bills", side_effect=RuntimeError("down")):
            self.sap_is_back()
        self.assertEqual(self.served().outcome, ServedBillOutcome.PENDING)

        self.take_copy(timezone.make_aware(datetime(2026, 9, 30, 11, 15)))

        self.assertEqual(self.served().outcome, ServedBillOutcome.CANCELLED)

    def test_a_bill_served_again_after_its_check_is_a_new_record(self):
        self.serve_while_down()
        self.sap_is_back()
        self.serve_while_down()

        self.assertEqual(
            sorted(ServedBill.objects.values_list("outcome", flat=True)),
            [ServedBillOutcome.PENDING, ServedBillOutcome.UNCHANGED],
        )
