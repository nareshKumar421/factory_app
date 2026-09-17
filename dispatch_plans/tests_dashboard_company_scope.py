"""
Scoping the dispatch dashboard's aggregation to named companies.

The summary endpoint aggregates over every company the caller belongs to, which
is the wrong default for a board that names the companies it adds up: a wall
headed "Oil + Mart" was folding in Beverages for anyone holding all three.

`companies=` narrows it. The property under test is that it can only ever
REMOVE a company — it is a scope control, never a way to read a company the
caller has no membership in.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from company.models import Company, UserCompany, UserRole

from .dashboard_views import _user_companies


class DashboardCompanyScopeTests(TestCase):
    def setUp(self):
        # DRF's factory, wrapped in a DRF Request below: `_user_companies` reads
        # `query_params`, which only exists on the DRF request the APIView sees.
        self.factory = APIRequestFactory()

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.beverages = Company.objects.create(
            name="Jivo Beverages", code="JIVO_BEVERAGES"
        )

        role = UserRole.objects.create(name="DispatchScopeViewer")
        self.user = get_user_model().objects.create_user(
            email="scope@example.com",
            password="testpass123",
            full_name="Scope Tester",
            employee_code="SCOPE01",
        )
        # Belongs to all three.
        for company in (self.oil, self.mart, self.beverages):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_active=True
            )

    def _codes(self, query=""):
        request = Request(self.factory.get(f"/?{query}"))
        request.user = self.user
        _ids, codes = _user_companies(request)
        return sorted(codes)

    def test_no_parameter_keeps_every_membership(self):
        """The historic behaviour: omitting the parameter changes nothing."""
        self.assertEqual(
            self._codes(),
            ["JIVO_BEVERAGES", "JIVO_MART", "JIVO_OIL"],
        )

    def test_parameter_narrows_to_the_named_companies(self):
        """The board's actual case — Beverages must drop out."""
        self.assertEqual(
            self._codes("companies=JIVO_OIL,JIVO_MART"),
            ["JIVO_MART", "JIVO_OIL"],
        )

    def test_a_single_company_narrows_to_one(self):
        self.assertEqual(self._codes("companies=JIVO_MART"), ["JIVO_MART"])

    def test_codes_are_matched_case_insensitively_and_trimmed(self):
        self.assertEqual(
            self._codes("companies= jivo_oil , JIVO_mart "),
            ["JIVO_MART", "JIVO_OIL"],
        )

    def test_cannot_widen_beyond_the_callers_memberships(self):
        """The security property: asking for a company you do not hold adds nothing."""
        outsider = Company.objects.create(name="Someone Else", code="OTHER_CO")
        self.assertTrue(outsider.pk)

        codes = self._codes("companies=JIVO_OIL,OTHER_CO")

        self.assertEqual(codes, ["JIVO_OIL"])
        self.assertNotIn("OTHER_CO", codes)

    def test_a_membership_the_caller_lacks_is_never_reachable(self):
        """Even asking for ONLY a company the caller lacks must not return it."""
        Company.objects.create(name="Foreign", code="FOREIGN_CO")

        codes = self._codes("companies=FOREIGN_CO")

        self.assertNotIn("FOREIGN_CO", codes)

    def test_a_parameter_matching_nothing_falls_back_to_every_membership(self):
        """
        A board asking for a company the viewer cannot see should show what it
        is allowed to rather than an empty screen it cannot explain. The
        fallback is deliberately the full list, never the empty set — an empty
        company list would aggregate to a silent zero.
        """
        self.assertEqual(
            self._codes("companies=FOREIGN_CO"),
            ["JIVO_BEVERAGES", "JIVO_MART", "JIVO_OIL"],
        )

    def test_an_empty_parameter_is_treated_as_absent(self):
        self.assertEqual(
            self._codes("companies="),
            ["JIVO_BEVERAGES", "JIVO_MART", "JIVO_OIL"],
        )

    def test_inactive_memberships_stay_excluded_when_scoping(self):
        """Scoping must not resurrect a membership that was switched off."""
        UserCompany.objects.filter(user=self.user, company=self.mart).update(
            is_active=False
        )

        self.assertEqual(self._codes("companies=JIVO_OIL,JIVO_MART"), ["JIVO_OIL"])
