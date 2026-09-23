"""
Shared fixtures for the leave suite.

A small real org rather than two employees in isolation, because almost every
rule in this module is about *position*: who may decide, whose subtree a
request falls in, what a skip-level approval is. Those cannot be exercised
against a flat pair.

    hr_admin  (login, no employee record -- HR acts on everybody)

    ceo ── head ── manager ── worker
                        └──── worker_two
            other_head ── other_worker      (a separate line, same company)

Everyone except ``worker_two`` and ``other_worker`` has a login, which also
mirrors the live data: about half the workforce has no way to sign in, so
"raised on their behalf" has to be exercised as the normal case it is.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from company.models import Company
from employee_hierarchy import hierarchy
from employee_hierarchy.constants import EmploymentStatus
from employee_hierarchy.models import Department, Designation, Employee

from .constants import RecordStatus
from .models import LeaveType

User = get_user_model()

#: A Wednesday, so a five-day span from here runs into a weekend and the
#: weekly-off rule is exercised by ordinary dates rather than a special case.
WEDNESDAY = date(2026, 10, 7)


class LeaveTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Jivo Oil", code="JIVO_OIL", is_active=True
        )
        cls.other_company = Company.objects.create(
            name="Jivo Mart", code="JIVO_MART", is_active=True
        )
        cls.department = Department.objects.create(
            company=cls.company, code="PROD", name="Production"
        )
        cls.other_department = Department.objects.create(
            company=cls.company, code="QA", name="Quality"
        )
        cls.designation = Designation.objects.create(
            company=cls.company, code="OP", name="Operator", level=1
        )

        cls.ceo = cls._employee("JWPL0001", "Chief", manager=None)
        cls.head = cls._employee("JWPL0002", "Head", manager=cls.ceo)
        cls.manager = cls._employee("JWPL0003", "Manager", manager=cls.head)
        cls.worker = cls._employee("JWPL0004", "Worker", manager=cls.manager)
        cls.worker_two = cls._employee("JWPL0005", "WorkerTwo", manager=cls.manager)
        cls.other_head = cls._employee(
            "JWPL0006", "OtherHead", manager=cls.ceo, department=cls.other_department
        )
        cls.other_worker = cls._employee(
            "JWPL0007",
            "OtherWorker",
            manager=cls.other_head,
            department=cls.other_department,
        )

        # Logins. worker_two and other_worker deliberately have none.
        cls.ceo_user = cls._login("ceo@test.local", cls.ceo)
        cls.head_user = cls._login("head@test.local", cls.head)
        cls.manager_user = cls._login("manager@test.local", cls.manager)
        cls.worker_user = cls._login("worker@test.local", cls.worker)
        cls.other_head_user = cls._login("otherhead@test.local", cls.other_head)

        # HR: a login with no employee record at all, which is how the real
        # HR accounts look.
        cls.hr_user = User.objects.create_user(
            email="hr@test.local", password="pw", full_name="HR"
        )

        cls.casual = LeaveType.objects.create(
            company=cls.company,
            code="CL",
            name="Casual Leave",
            allow_half_day=True,
            annual_quota=12,
        )
        cls.sick = LeaveType.objects.create(
            company=cls.company,
            code="SL",
            name="Sick Leave",
            allow_half_day=False,
            requires_document=True,
            annual_quota=10,
        )
        cls.retired_type = LeaveType.objects.create(
            company=cls.company,
            code="OLD",
            name="Discontinued Leave",
            status=RecordStatus.INACTIVE,
        )

    # -- helpers -------------------------------------------------------------

    @classmethod
    def _employee(cls, code, name, *, manager, department=None, **kwargs):
        employee = Employee.objects.create(
            company=cls.company,
            employee_code=code,
            first_name=name,
            department=department or cls.department,
            designation=cls.designation,
            employment_status=kwargs.pop("employment_status", EmploymentStatus.ACTIVE),
            **kwargs,
        )
        hierarchy.place(employee, manager)
        return employee

    @classmethod
    def _login(cls, email, employee):
        user = User.objects.create_user(email=email, password="pw", full_name=email)
        employee.user = user
        employee.save(update_fields=["user"])
        return user

    @staticmethod
    def grant(user, *codenames):
        """Give a user permissions by codename, e.g. ``'leave.can_decide_leave'``."""
        for dotted in codenames:
            app_label, codename = dotted.split(".", 1)
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        # ModelBackend memoises on the instance. The caches have to be
        # *removed*, not blanked -- it tests `hasattr`, so a None left behind
        # reads as "already resolved, and the answer is nothing".
        for attribute in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            if hasattr(user, attribute):
                delattr(user, attribute)
        return user
