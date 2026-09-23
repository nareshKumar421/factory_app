"""Shared fixture: one company, one user, and whatever rights a test grants."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from construction_projects.models import Project

BASE = "/api/v1/construction/"

ALL_PERMISSIONS = [
    "can_view_project",
    "can_view_all_projects",
    "can_create_project",
    "can_edit_project",
    "can_approve_project",
    "can_log_daily_work",
    "can_record_expense",
    "can_approve_expense",
    "can_close_project",
]


class ConstructionTestCase(APITestCase):
    #: Subclasses narrow this to test that a right is actually required.
    permissions = ALL_PERMISSIONS

    def setUp(self):
        self.today = timezone.localdate()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="site@example.com",
            password="testpass",
            full_name="Site Engineer",
            employee_code="CON001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Engineer")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.grant(self.user, self.permissions)

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}

    # -- helpers -----------------------------------------------------------

    def grant(self, user, codenames):
        if not codenames:
            return
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="construction_projects",
                codename__in=codenames,
            )
        )
        # has_perm() caches per instance; drop the caches so the next call
        # sees what we just granted.
        for attr in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            user.__dict__.pop(attr, None)

    def make_user(self, email, code, permissions=(), company=None):
        user = get_user_model().objects.create_user(
            email=email, password="testpass", full_name=email, employee_code=code
        )
        UserCompany.objects.create(
            user=user, company=company or self.company, role=self.role
        )
        self.grant(user, list(permissions))
        return user

    def project_payload(self, **overrides):
        payload = {
            "name": "New packing shed, Block C",
            "location": "Block C, north side",
            "start_date": str(self.today - timedelta(days=10)),
            "expected_end_date": str(self.today + timedelta(days=50)),
            "estimated_cost": "800000.00",
            "manager": self.user.id,
            "site_incharge": self.user.id,
        }
        payload.update(overrides)
        return payload

    def make_project(self, *, approved=True, company=None, **overrides):
        """A project straight in the database, past its approval if asked."""
        data = {
            "name": "Shed",
            "location": "Block C",
            "start_date": self.today - timedelta(days=10),
            "expected_end_date": self.today + timedelta(days=50),
            "estimated_cost": Decimal("800000.00"),
            "manager": self.user,
            "site_incharge": self.user,
        }
        data.update(overrides)
        from construction_projects import services

        project = services.create_project(
            company=company or self.company, user=self.user, **data
        )
        if approved:
            services.submit_project(project, user=self.user)
            services.approve_project(project, user=self.user)
        return project

    # -- request shorthands ------------------------------------------------

    def get(self, path, **params):
        return self.client.get(f"{BASE}{path}", params, **self.headers)

    def post(self, path, payload=None, *, fmt="json"):
        return self.client.post(
            f"{BASE}{path}", payload or {}, format=fmt, **self.headers
        )

    def patch(self, path, payload=None):
        return self.client.patch(
            f"{BASE}{path}", payload or {}, format="json", **self.headers
        )

    def delete(self, path):
        return self.client.delete(f"{BASE}{path}", **self.headers)

    def assertCode(self, response, expected_code):
        """Assert the module's stable error code, not the prose."""
        self.assertEqual(
            response.data.get("code"),
            expected_code,
            f"expected {expected_code}, got {response.data}",
        )

    def refreshed(self, project):
        return Project.objects.get(pk=project.pk)
