"""
Quota enforcement, required paperwork, and the overdraw escape hatch.

    python manage.py test leave.tests_quota --settings=config.sqlite_test_settings

The rule these pin down: a quota is a *ceiling on what may be asked for*, not a
report produced afterwards. Before this, `annual_quota` was documentation --
the balance endpoint reported it and nothing refused an application that blew
straight past it.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .constants import DayPortion
from .services import LeaveRefused, apply_for_leave, approve
from .tests_api import LeaveAPITestBase
from .tests_base import WEDNESDAY, LeaveTestBase


class QuotaTests(LeaveTestBase):
    def _apply(self, **kwargs):
        params = {
            "employee": self.worker,
            "leave_type": self.casual,
            "from_date": WEDNESDAY,
            "to_date": WEDNESDAY,
            "reason": "Reason",
            "applied_by": self.worker_user,
        }
        params.update(kwargs)
        return apply_for_leave(**params)

    def test_applying_within_the_quota_is_fine(self):
        self.casual.annual_quota = 5
        self.casual.save(update_fields=["annual_quota"])
        request = self._apply(to_date=WEDNESDAY + timedelta(days=2))
        self.assertEqual(request.total_days, Decimal("3.0"))

    def test_applying_past_the_quota_is_refused(self):
        self.casual.annual_quota = 2
        self.casual.save(update_fields=["annual_quota"])
        with self.assertRaisesMessage(LeaveRefused, "day(s) left in"):
            self._apply(to_date=WEDNESDAY + timedelta(days=4))

    def test_the_refusal_says_what_is_left_and_what_was_asked(self):
        self.casual.annual_quota = 2
        self.casual.save(update_fields=["annual_quota"])
        with self.assertRaises(LeaveRefused) as caught:
            self._apply(to_date=WEDNESDAY + timedelta(days=4))
        message = str(caught.exception)
        self.assertIn("2 day(s) left", message)
        # WEDNESDAY + 4 spans a Sunday, so the ask is 4 working days.
        self.assertIn("asks for 4", message)

    def test_approved_days_eat_into_the_quota(self):
        self.casual.annual_quota = 3
        self.casual.save(update_fields=["annual_quota"])
        first = self._apply(to_date=WEDNESDAY + timedelta(days=1))
        approve(first, user=self.manager_user)

        with self.assertRaisesMessage(LeaveRefused, "day(s) left in"):
            self._apply(
                from_date=WEDNESDAY + timedelta(days=7),
                to_date=WEDNESDAY + timedelta(days=8),
            )

    def test_pending_days_are_reserved_too(self):
        """Two pending requests must not both spend the same day."""
        self.casual.annual_quota = 2
        self.casual.save(update_fields=["annual_quota"])
        self._apply(to_date=WEDNESDAY + timedelta(days=1))  # 2 days, pending

        with self.assertRaisesMessage(LeaveRefused, "awaiting approval"):
            self._apply(from_date=WEDNESDAY + timedelta(days=7), to_date=WEDNESDAY + timedelta(days=7))

    def test_a_rejected_request_frees_the_quota_again(self):
        from .services import reject

        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])
        first = self._apply()
        reject(first, user=self.manager_user, comment="No")

        later = self._apply(from_date=WEDNESDAY + timedelta(days=7), to_date=WEDNESDAY + timedelta(days=7))
        self.assertEqual(later.total_days, Decimal("1.0"))

    def test_a_half_day_only_spends_half_the_quota(self):
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])
        first = self._apply(portion=DayPortion.FIRST_HALF)
        approve(first, user=self.manager_user)
        # Half a day remains, so another half day fits.
        second = self._apply(
            from_date=WEDNESDAY + timedelta(days=7),
            to_date=WEDNESDAY + timedelta(days=7),
            portion=DayPortion.FIRST_HALF,
        )
        self.assertEqual(second.total_days, Decimal("0.5"))

    def test_an_untracked_type_has_no_ceiling(self):
        self.casual.annual_quota = 0
        self.casual.save(update_fields=["annual_quota"])
        request = self._apply(to_date=WEDNESDAY + timedelta(days=20))
        self.assertGreater(request.total_days, Decimal("10"))

    def test_allow_overdraw_bypasses_the_quota(self):
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])
        # WEDNESDAY + 4 spans a Sunday: five calendar days, four working ones.
        request = self._apply(to_date=WEDNESDAY + timedelta(days=4), allow_overdraw=True)
        self.assertEqual(request.total_days, Decimal("4.0"))

    def test_a_span_crossing_new_year_is_charged_to_both_years(self):
        """Checking only the start year would spend next year's entitlement here."""
        from datetime import date

        self.casual.annual_quota = 2
        self.casual.save(update_fields=["annual_quota"])

        # Two days in December, using up that year.
        december = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=date(2026, 12, 21),
            to_date=date(2026, 12, 22),
            reason="December",
            applied_by=self.worker_user,
        )
        approve(december, user=self.manager_user)

        # A span starting in the fresh year is fine...
        january = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=date(2027, 1, 5),
            to_date=date(2027, 1, 6),
            reason="January",
            applied_by=self.worker_user,
        )
        self.assertEqual(january.total_days, Decimal("2.0"))

    def test_the_quota_is_per_leave_type(self):
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])
        first = self._apply()
        approve(first, user=self.manager_user)
        # Sick leave has its own 10, untouched by the casual leave above.
        other = self._apply(
            leave_type=self.sick,
            from_date=WEDNESDAY + timedelta(days=7),
            to_date=WEDNESDAY + timedelta(days=7),
            document=SimpleUploadedFile("note.pdf", b"%PDF-1.4 certificate"),
        )
        self.assertEqual(other.total_days, Decimal("1.0"))


class RequiredDocumentTests(LeaveTestBase):
    def test_a_type_that_requires_paperwork_refuses_without_it(self):
        with self.assertRaisesMessage(LeaveRefused, "supporting paperwork"):
            apply_for_leave(
                employee=self.worker,
                leave_type=self.sick,  # requires_document=True
                from_date=WEDNESDAY,
                to_date=WEDNESDAY,
                reason="Not well",
                applied_by=self.worker_user,
            )

    def test_with_a_document_it_goes_through(self):
        request = apply_for_leave(
            employee=self.worker,
            leave_type=self.sick,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Not well",
            document=SimpleUploadedFile("certificate.pdf", b"%PDF-1.4 doctor"),
            applied_by=self.worker_user,
        )
        self.assertTrue(request.document)

    def test_a_type_that_does_not_require_one_is_unaffected(self):
        request = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Family",
            applied_by=self.worker_user,
        )
        self.assertFalse(request.document)


class QuotaAPITests(LeaveAPITestBase):
    def test_the_quota_refusal_reaches_the_client_as_400(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])

        response = self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY + timedelta(days=3)),
                "reason": "Too much",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("day(s) left in", response.data["detail"])

    def test_hr_may_deliberately_overdraw(self):
        self.grant(
            self.hr_user, "leave.can_apply_leave_for_others", "leave.can_decide_any_leave"
        )
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])

        response = self.client_for(self.hr_user).post(
            reverse("leave-request-list"),
            {
                "employee": self.worker.pk,
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY + timedelta(days=3)),
                "reason": "Compassionate, agreed with the plant head",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_certificate_can_actually_be_uploaded(self):
        """A FileField is useless if the endpoint only parses JSON."""
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.sick.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Not well",
                "document": SimpleUploadedFile("cert.pdf", b"%PDF-1.4 doctor"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["document"])

    def test_the_list_is_bounded(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(
            reverse("leave-request-list"), {"limit": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.data), 1)
