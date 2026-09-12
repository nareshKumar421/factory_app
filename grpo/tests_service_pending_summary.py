"""The pending service-GRPO summary the control boards age their queue from.

The bucketing itself is straightforward. What these guard is the distinction
the buckets have to preserve: freight is typed on the post form *after* the
bilty exists, so a band can hold a hundred receipts and no money at all. A
bucket that reports only ``amount`` cannot tell that apart from freight that
genuinely costs nothing, and the wall board then renders "170" over "₹0" --
a cell that reads as settled when nothing in it has been priced.

Run with::

    python manage.py test grpo.tests_service_pending_summary         --settings=config.sqlite_test_settings
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from dispatch_plans.models import DispatchPlan
from grpo.views_service_pending_summary import ServicePendingSummaryAPI


class _StubCompany:
    """Just enough of the company-context middleware for the view to read."""

    def __init__(self, code):
        self.company = type("C", (), {"code": code})()


class _StubRequest:
    def __init__(self, code):
        self.company = _StubCompany(code)


class ServicePendingSummaryBucketTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART_T")
        self.user = get_user_model().objects.create_user(
            email="sg@example.com",
            password="p",
            full_name="SG",
            employee_code="SG1",
        )
        self.today = date.today()

    def _plan(self, entry, *, age_days, freight=None):
        """A dispatched plan of a given age, priced or not."""
        from django.core.files.base import ContentFile

        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=entry,
            sap_invoice_doc_num=str(entry),
            booking_status="DISPATCHED",
            dispatch_date=self.today - timedelta(days=age_days),
            bilty_no=f"BL{entry}",
            total_freight=freight,
            created_by=self.user,
            updated_by=self.user,
        )
        plan.bilty_attachment.save(f"b{entry}.pdf", ContentFile(b"x"), save=True)
        return plan

    def _summary(self):
        response = ServicePendingSummaryAPI().get(_StubRequest(self.company.code))
        return response.data

    def _bucket(self, data, band):
        return next(b for b in data["buckets"] if b["band"] == band)

    def test_a_band_of_unpriced_receipts_says_so_rather_than_reporting_no_money(self):
        """The failure this exists to prevent: ₹0 against a queue nobody priced."""
        self._plan(9101, age_days=2)
        self._plan(9102, age_days=5)

        fresh = self._bucket(self._summary(), 0)

        self.assertEqual(fresh["documents"], 2)
        self.assertEqual(fresh["amount"], 0.0)
        # Without this the cell is indistinguishable from freight worth nothing.
        self.assertEqual(fresh["unpriced"], 2)

    def test_a_part_priced_band_reports_both_the_money_and_the_gap(self):
        self._plan(9111, age_days=20, freight=5_000)
        self._plan(9112, age_days=20)

        band = self._bucket(self._summary(), 15)

        self.assertEqual(band["documents"], 2)
        self.assertEqual(band["amount"], 5_000.0)
        self.assertEqual(band["unpriced"], 1)

    def test_a_fully_priced_band_reports_nothing_unpriced(self):
        self._plan(9121, age_days=50, freight=7_500)

        band = self._bucket(self._summary(), 45)

        self.assertEqual(band["amount"], 7_500.0)
        self.assertEqual(band["unpriced"], 0)

    def test_the_buckets_account_for_every_dated_document(self):
        """Buckets are exclusive, so they must add back to the dated total."""
        self._plan(9131, age_days=1)
        self._plan(9132, age_days=20, freight=1_000)
        self._plan(9133, age_days=35)
        self._plan(9134, age_days=60, freight=2_000)

        data = self._summary()

        self.assertEqual(sum(b["documents"] for b in data["buckets"]), 4)
        self.assertEqual([b["band"] for b in data["buckets"]], [0, 15, 30, 45])

    def test_the_column_total_and_the_buckets_agree_on_what_is_unpriced(self):
        """A header disagreeing with the table under it is worse than neither."""
        self._plan(9141, age_days=1)
        self._plan(9142, age_days=20, freight=1_000)
        self._plan(9143, age_days=60)

        data = self._summary()

        self.assertEqual(sum(b["unpriced"] for b in data["buckets"]), data["unpriced"])
        self.assertEqual(data["unpriced"], 2)
