"""What a control board may read across companies, and what it still may not.

The Logistics Control wall reports Oil and Mart added together. It gets there by
asking each feed once per company with the ``Company-Code`` header pinned, so a
login that is not a member of Mart used to have every Mart leg 403 and the board
quietly showed Oil alone under a heading that says both.

``HasBoardCompanyContext`` is the answer: the company gate widens for those
reads, the module gate does not. The tests worth having are therefore mostly the
negative ones — the limits are the half that rots when somebody reuses this
class on a view it was never meant for.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

User = get_user_model()

STOCK_RIGHT = ("stock_dashboard", "can_view_stock_dashboard")


def stock_permission():
    app_label, codename = STOCK_RIGHT
    return Permission.objects.get(
        content_type__app_label=app_label, codename=codename
    )


class BoardCompanyContextTests(TestCase):
    """An Oil member holding the stock right, asking for Mart."""

    def setUp(self):
        self.client = APIClient()
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        # A company the boards never add in. Membership is the only way to it.
        self.outsider = Company.objects.create(name="Some Other Co", code="OTHER_CO")
        self.role, _ = UserRole.objects.get_or_create(name="Warehouse")

        self.user = User.objects.create_user(
            email="oil.only@example.com", full_name="Oil Only", password="x"
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.oil,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        self.user.user_permissions.add(stock_permission())
        self.client.force_authenticate(user=self.user)

    def get(self, name, company_code, **params):
        self.client.credentials(HTTP_COMPANY_CODE=company_code)
        return self.client.get(reverse(name), params)

    # ------------------------------------------------------------------ allow

    def test_a_board_feed_answers_for_a_company_the_user_is_not_in(self):
        """The change itself. 403 here is the bug this class exists to fix."""
        response = self.get("owned-vehicle-status", "JIVO_MART")
        self.assertNotEqual(response.status_code, 403, response.content[:400])

    def test_the_user_s_own_company_is_unaffected(self):
        response = self.get("owned-vehicle-status", "JIVO_OIL")
        self.assertNotEqual(response.status_code, 403, response.content[:400])

    # ------------------------------------------------------------------ limits

    def test_the_module_right_is_still_required(self):
        """Widening the COMPANY gate must not open a report on its own."""
        self.user.user_permissions.clear()
        # `has_perm` caches on the instance, so re-authenticate with a fresh one.
        self.client.force_authenticate(user=User.objects.get(pk=self.user.pk))
        self.assertEqual(self.get("owned-vehicle-status", "JIVO_MART").status_code, 403)

    def test_writing_another_company_s_data_is_still_refused(self):
        """Reads only. Membership is what decides where somebody may act."""
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        response = self.client.put(
            f"{reverse('warehouse-board-settings')}?warehouse=BH-BT",
            {"capacity_tonnes": 1000},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content[:400])

    def test_a_company_outside_the_board_set_is_still_refused(self):
        self.assertEqual(
            self.get("owned-vehicle-status", "OTHER_CO").status_code, 403
        )

    def test_an_unknown_company_code_is_still_refused(self):
        self.assertEqual(
            self.get("owned-vehicle-status", "JIVO_BEVERAGES").status_code,
            403,
            "a code not configured on this deployment must not resolve",
        )

    def test_a_login_belonging_to_no_company_is_still_refused(self):
        """A widening for staff, never for any authenticated account."""
        outsider = User.objects.create_user(
            email="nobody@example.com", full_name="No Company", password="x"
        )
        outsider.user_permissions.add(stock_permission())
        self.client.force_authenticate(user=outsider)
        self.assertEqual(self.get("owned-vehicle-status", "JIVO_MART").status_code, 403)

    def test_an_inactive_membership_does_not_count_as_belonging(self):
        UserCompany.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(self.get("owned-vehicle-status", "JIVO_MART").status_code, 403)

    def test_a_missing_header_is_still_refused(self):
        self.client.credentials()
        self.assertEqual(
            self.client.get(reverse("owned-vehicle-status")).status_code, 403
        )

    # ------------------------------------------------- the un-widened default

    def test_an_ordinary_endpoint_still_refuses_the_cross_company_read(self):
        """The plain `HasCompanyContext` is unchanged.

        `stock-dashboard` is the same app, the same right and the same header —
        the only difference is that no board fans it out across companies, so it
        keeps the membership rule. If this stops being a 403, the widening has
        escaped the endpoints it was scoped to.
        """
        self.assertEqual(self.get("stock-dashboard", "JIVO_MART").status_code, 403)
