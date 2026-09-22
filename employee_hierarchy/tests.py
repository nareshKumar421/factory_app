"""
Tests for the employee hierarchy and compensation module.

Three things are worth pinning down, and they are the three things that would
be expensive to get wrong.

**The tree's rules.** A cycle, a self-report or a move that orphans a team is a
corrupt org chart, and a corrupt org chart is discovered weeks later by
somebody who cannot work out who approves their leave. So: no self-reporting,
no loops at any depth, a manager who moves takes their team, a manager who
moves *without* their team leaves it with somebody real, and no assigning
people to a suspended or departed manager.

**Salary history.** The brief's central promise is that a revision never
overwrites what came before. The tests hold three revisions on one employee and
check that all three are still readable, that exactly one is in force, and that
a future-dated one waits for its date.

**Salary privacy.** The rules that would leak if they were subtly wrong: an
employee sees their own figure and nothing else; a manager sees below them but
not their own manager and not their peers; a department head sees their
department and its sub-departments; and filtering or sorting by salary -- which
is a way of *reading* salary -- is refused to anyone without the right.
"""

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase

from accounts.models import Department as OrgDepartment
from company.models import Company, UserCompany, UserRole

from . import hierarchy, services
from .constants import EmploymentStatus, SalaryStatus
from .models import (
    Branch,
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    PermanentLabourAudit,
    PermanentLabourPresence,
    PermanentLabourStrength,
)

User = get_user_model()

BASE = "/api/v1/employee-hierarchy"
OIL = "JIVO_OIL"
MART = "JIVO_MART"


def _companies():
    oil, _ = Company.objects.get_or_create(code=OIL, defaults={"name": "Jivo Oil"})
    mart, _ = Company.objects.get_or_create(code=MART, defaults={"name": "Jivo Mart"})
    return oil, mart


def _user(*codenames, employee=None):
    """A user holding the given module permissions, in both companies."""
    count = User.objects.count()
    user = User.objects.create_user(
        email=f"hr{count}@t.com",
        password="x",
        full_name=f"HR User {count}",
        employee_code=f"U{count}",
    )
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="employee_hierarchy", codename__in=codenames
        )
    )
    role, _ = UserRole.objects.get_or_create(name="Admin")
    for company in _companies():
        UserCompany.objects.get_or_create(user=user, company=company, role=role)
    user = User.objects.get(pk=user.pk)
    if employee is not None:
        employee.user = user
        employee.save(update_fields=["user"])
    return user


def _client(user, company=OIL):
    client = APIClient(headers={"Company-Code": company})
    client.force_authenticate(user=user)
    return client


class OrgFixture(APITestCase):
    """The org the brief draws, small enough to assert about.

    ``ceo → cto → eng_manager → senior_dev → {dev_one, dev_two}`` plus
    ``qa_manager`` under the CTO and ``hr_head`` under the CEO. Built through
    the services, so the fixture itself exercises the placement code.
    """

    def setUp(self):
        self.oil, self.mart = _companies()
        self.tech = Department.objects.create(company=self.oil, code="TECH", name="Technology")
        self.engineering = Department.objects.create(
            company=self.oil, code="ENG", name="Engineering", parent=self.tech
        )
        self.quality = Department.objects.create(
            company=self.oil, code="QA", name="QA", parent=self.tech
        )
        self.people_department = Department.objects.create(
            company=self.oil, code="HR", name="Human Resources"
        )
        self.manager_rung = Designation.objects.create(
            company=self.oil, code="MGR", name="Manager", level=5, is_managerial=True
        )
        self.dev_rung = Designation.objects.create(
            company=self.oil, code="DEV", name="Developer", level=8
        )

        self.ceo = self._hire("EMP001", "Arun", None, self.tech)
        self.cto = self._hire("EMP002", "Priya", self.ceo, self.tech)
        self.eng_manager = self._hire("EMP003", "Sandeep", self.cto, self.engineering)
        self.senior_dev = self._hire("EMP004", "Kavya", self.eng_manager, self.engineering)
        self.dev_one = self._hire("EMP005", "Rohit", self.senior_dev, self.engineering)
        self.dev_two = self._hire("EMP006", "Neha", self.senior_dev, self.engineering)
        self.qa_manager = self._hire("EMP007", "Meera", self.cto, self.quality)
        self.hr_head = self._hire("EMP008", "Rajesh", self.ceo, self.people_department)

    def _hire(self, code, name, manager, department, *, company=None, status_value=None):
        return services.create_employee(
            company=company or self.oil,
            data={
                "employee_code": code,
                "first_name": name,
                "last_name": "Test",
                "email": f"{code.lower()}@example.com",
                "joining_date": date(2020, 1, 1),
                "department": department,
                "designation": self.dev_rung,
                "reporting_manager": manager,
                "employment_status": status_value or EmploymentStatus.ACTIVE,
            },
        )

    def _pay(self, employee, amount, effective_from, *, approve=True):
        return services.create_salary_record(
            employee,
            effective_from=effective_from,
            basic_salary=Decimal(amount),
            currency="INR",
            approve=approve,
        )


class HierarchyPathTests(OrgFixture):
    def test_paths_and_levels_follow_the_chain(self):
        self.assertEqual(self.ceo.hierarchy_path, f"/{self.ceo.pk}/")
        self.assertEqual(self.ceo.hierarchy_level, 1)
        self.assertEqual(
            self.dev_one.hierarchy_path,
            f"/{self.ceo.pk}/{self.cto.pk}/{self.eng_manager.pk}/{self.senior_dev.pk}/{self.dev_one.pk}/",
        )
        self.assertEqual(self.dev_one.hierarchy_level, 5)

    def test_subtree_is_everyone_below_at_any_depth(self):
        under_cto = set(hierarchy.subtree(self.cto).values_list("employee_code", flat=True))
        self.assertEqual(under_cto, {"EMP003", "EMP004", "EMP005", "EMP006", "EMP007"})
        self.assertEqual(hierarchy.subtree(self.dev_one).count(), 0)

    def test_reporting_chain_reads_top_down(self):
        chain = [person.employee_code for person in hierarchy.reporting_chain(self.dev_one)]
        self.assertEqual(chain, ["EMP001", "EMP002", "EMP003", "EMP004"])

    def test_peers_are_the_others_under_the_same_manager(self):
        peers = set(hierarchy.peers(self.dev_one).values_list("employee_code", flat=True))
        self.assertEqual(peers, {"EMP006"})
        # A top-level employee has no peers, rather than "every other root".
        self.assertEqual(hierarchy.peers(self.ceo).count(), 0)

    def test_rebuild_repairs_paths_edited_behind_the_services(self):
        Employee.objects.filter(pk=self.dev_one.pk).update(
            hierarchy_path="/nonsense/", hierarchy_level=9
        )
        hierarchy.rebuild_paths(self.oil)
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.hierarchy_level, 5)
        self.assertTrue(self.dev_one.hierarchy_path.endswith(f"/{self.dev_one.pk}/"))


class HierarchyRuleTests(OrgFixture):
    def test_nobody_reports_to_themselves(self):
        with self.assertRaises(ValidationError):
            services.change_manager(self.cto, self.cto)

    def test_a_loop_is_refused_at_any_depth(self):
        # The CEO cannot be made to report to somebody three levels below them.
        with self.assertRaises(ValidationError):
            services.change_manager(self.ceo, self.dev_one)
        # Nor to their own direct report.
        with self.assertRaises(ValidationError):
            services.change_manager(self.ceo, self.cto)

    def test_a_manager_who_moves_takes_their_team(self):
        result = services.change_manager(self.senior_dev, self.qa_manager, reason="Reorg")
        self.assertEqual(result["team_moved"], 2)

        self.senior_dev.refresh_from_db()
        self.dev_one.refresh_from_db()
        self.dev_two.refresh_from_db()
        self.assertEqual(self.senior_dev.reporting_manager_id, self.qa_manager.pk)
        self.assertEqual(self.dev_one.reporting_manager_id, self.senior_dev.pk)
        self.assertTrue(self.dev_one.hierarchy_path.startswith(self.qa_manager.path_prefix))
        # Levels shift with the move: QA manager is at 3, so the devs land at 5.
        self.assertEqual(self.senior_dev.hierarchy_level, 4)
        self.assertEqual(self.dev_one.hierarchy_level, 5)
        self.assertEqual(self.dev_two.hierarchy_level, 5)

    def test_moving_without_the_team_leaves_it_one_level_up(self):
        services.change_manager(
            self.senior_dev, self.qa_manager, carry_team=False, reason="Individual move"
        )
        self.dev_one.refresh_from_db()
        self.dev_two.refresh_from_db()
        # They now report to the manager the senior developer left behind.
        self.assertEqual(self.dev_one.reporting_manager_id, self.eng_manager.pk)
        self.assertEqual(self.dev_two.reporting_manager_id, self.eng_manager.pk)
        self.assertEqual(self.dev_one.hierarchy_level, 4)

    def test_a_top_level_employee_cannot_abandon_their_team(self):
        with self.assertRaises(ValidationError):
            services.change_manager(self.ceo, self.qa_manager, carry_team=False)

    def test_a_suspended_manager_cannot_be_assigned(self):
        services.change_status(self.qa_manager, EmploymentStatus.SUSPENDED)
        self.qa_manager.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.change_manager(self.dev_one, self.qa_manager)

    def test_a_departed_manager_cannot_be_assigned(self):
        services.change_status(self.qa_manager, EmploymentStatus.RESIGNED)
        self.qa_manager.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.change_manager(self.dev_one, self.qa_manager)

    def test_a_manager_from_another_company_is_refused(self):
        other = self._hire("MART001", "Other", None, None, company=self.mart)
        with self.assertRaises(ValidationError):
            services.change_manager(self.dev_one, other)

    def test_removing_the_manager_makes_a_top_level_employee(self):
        services.change_manager(self.cto, None, reason="Now runs a separate unit")
        self.cto.refresh_from_db()
        self.eng_manager.refresh_from_db()
        self.assertIsNone(self.cto.reporting_manager_id)
        self.assertEqual(self.cto.hierarchy_level, 1)
        # The team came along, so the chain below stays intact.
        self.assertEqual(self.eng_manager.hierarchy_level, 2)
        self.assertEqual(
            Employee.objects.filter(company=self.oil, reporting_manager__isnull=True).count(), 2
        )

    def test_exit_moves_the_team_up_and_records_why(self):
        result = services.change_status(
            self.senior_dev, EmploymentStatus.RESIGNED, reason="Left for another company"
        )
        self.assertEqual(result["reports_reassigned"], 2)
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.reporting_manager_id, self.eng_manager.pk)
        self.senior_dev.refresh_from_db()
        self.assertIsNotNone(self.senior_dev.exit_date)
        self.assertTrue(
            EmployeeHistory.objects.filter(employee=self.dev_one, event="MANAGER_CHANGED").exists()
        )

    def test_the_manager_flag_turns_on_when_somebody_gains_a_report(self):
        newcomer = self._hire("EMP009", "Farhan", self.ceo, self.people_department)
        self.assertFalse(newcomer.is_manager)
        services.change_manager(self.dev_two, newcomer)
        newcomer.refresh_from_db()
        self.assertTrue(newcomer.is_manager)


class SalaryHistoryTests(OrgFixture):
    def test_a_revision_adds_a_record_and_keeps_the_old_one(self):
        self._pay(self.senior_dev, 500000, date(2024, 1, 1))
        self._pay(self.senior_dev, 600000, date(2025, 4, 1))
        self._pay(self.senior_dev, 720000, date(2026, 4, 1))

        records = self.senior_dev.salary_records.order_by("effective_from")
        self.assertEqual([record.total_compensation for record in records],
                         [Decimal("500000.00"), Decimal("600000.00"), Decimal("720000.00")])
        # Exactly one is in force, and it is the latest one that has started.
        active = records.filter(status=SalaryStatus.ACTIVE)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().effective_from, date(2026, 4, 1))
        self.assertEqual(
            records.filter(status=SalaryStatus.SUPERSEDED).count(), 2
        )

        self.senior_dev.refresh_from_db()
        self.assertEqual(self.senior_dev.current_salary_amount, Decimal("720000.00"))

    def test_each_revision_records_the_pair_of_figures(self):
        self._pay(self.senior_dev, 500000, date(2024, 1, 1))
        record = self._pay(self.senior_dev, 600000, date(2025, 4, 1))
        revision = record.revision
        self.assertEqual(revision.previous_amount, Decimal("500000.00"))
        self.assertEqual(revision.new_amount, Decimal("600000.00"))
        self.assertEqual(revision.change_amount, Decimal("100000.00"))
        self.assertEqual(round(revision.change_percent), 20)

    def test_a_future_revision_waits_for_its_date(self):
        today = timezone.localdate()
        self._pay(self.senior_dev, 500000, today - timedelta(days=30))
        future = self._pay(self.senior_dev, 800000, today + timedelta(days=30))
        future.refresh_from_db()
        self.assertEqual(future.status, SalaryStatus.SCHEDULED)
        self.senior_dev.refresh_from_db()
        self.assertEqual(self.senior_dev.current_salary_amount, Decimal("500000.00"))

        # When the date arrives, the daily command brings it into force.
        EmployeeSalary.objects.filter(pk=future.pk).update(
            effective_from=today - timedelta(days=1)
        )
        services.apply_due_revisions(self.oil)
        future.refresh_from_db()
        self.senior_dev.refresh_from_db()
        self.assertEqual(future.status, SalaryStatus.ACTIVE)
        self.assertEqual(self.senior_dev.current_salary_amount, Decimal("800000.00"))

    def test_history_cannot_be_back_dated_under_the_current_salary(self):
        self._pay(self.senior_dev, 600000, date(2025, 4, 1))
        with self.assertRaises(ValidationError):
            self._pay(self.senior_dev, 500000, date(2024, 1, 1))

    def test_two_records_cannot_start_on_the_same_day(self):
        self._pay(self.senior_dev, 600000, date(2025, 4, 1))
        with self.assertRaises(ValidationError):
            self._pay(self.senior_dev, 650000, date(2025, 4, 1))

    def test_an_unapproved_revision_is_not_the_salary(self):
        self._pay(self.senior_dev, 500000, date(2024, 1, 1))
        raises = EmployeeHistory.objects.filter(
            employee=self.senior_dev, event="SALARY_REVISED"
        )
        self.assertEqual(raises.count(), 1)

        proposed = self._pay(self.senior_dev, 900000, date(2025, 4, 1), approve=False)
        self.assertEqual(proposed.status, SalaryStatus.PENDING)
        self.senior_dev.refresh_from_db()
        self.assertEqual(self.senior_dev.current_salary_amount, Decimal("500000.00"))
        # A proposal nobody has approved must not appear in the employee's story
        # as a raise they did not get.
        self.assertEqual(raises.count(), 1)

        services.approve_salary_record(proposed)
        self.senior_dev.refresh_from_db()
        self.assertEqual(self.senior_dev.current_salary_amount, Decimal("900000.00"))
        self.assertEqual(raises.count(), 2)

    def test_a_rejected_revision_is_kept(self):
        proposed = self._pay(self.senior_dev, 900000, date(2025, 4, 1), approve=False)
        services.reject_salary_record(proposed, reason="Not this cycle")
        proposed.refresh_from_db()
        self.assertEqual(proposed.status, SalaryStatus.REJECTED)
        self.assertTrue(
            EmployeeAuditLog.objects.filter(
                employee=self.senior_dev, action="SALARY_REJECTED"
            ).exists()
        )

    def test_totals_are_derived_from_the_components(self):
        record = services.create_salary_record(
            self.senior_dev,
            effective_from=date(2025, 4, 1),
            basic_salary=Decimal("600000"),
            allowances=Decimal("150000"),
            bonuses=Decimal("50000"),
            deductions=Decimal("20000"),
            approve=True,
        )
        self.assertEqual(record.total_compensation, Decimal("780000.00"))


class SalaryPrivacyTests(OrgFixture):
    """Who may see whose pay -- the rules that would leak if they were wrong."""

    def setUp(self):
        super().setUp()
        for employee, amount in (
            (self.ceo, 5000000),
            (self.cto, 3000000),
            (self.eng_manager, 1800000),
            (self.senior_dev, 1000000),
            (self.dev_one, 600000),
            (self.qa_manager, 1400000),
        ):
            self._pay(employee, amount, date(2025, 4, 1))

    def _salary(self, client, employee):
        return client.get(f"{BASE}/employees/{employee.pk}/salary/")

    def test_an_employee_sees_their_own_and_nothing_else(self):
        user = _user("can_view_employees", "can_view_own_salary", employee=self.dev_one)
        client = _client(user)
        self.assertEqual(self._salary(client, self.dev_one).status_code, status.HTTP_200_OK)
        # Not their own manager's, even though they are in the same chain.
        self.assertEqual(
            self._salary(client, self.senior_dev).status_code, status.HTTP_403_FORBIDDEN
        )
        # And not a peer's.
        self.assertEqual(
            self._salary(client, self.dev_two).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_manager_sees_below_them_but_not_above_or_beside(self):
        user = _user(
            "can_view_employees",
            "can_view_own_salary",
            "can_view_subordinate_salary",
            employee=self.eng_manager,
        )
        client = _client(user)
        self.assertEqual(self._salary(client, self.senior_dev).status_code, status.HTTP_200_OK)
        self.assertEqual(self._salary(client, self.dev_one).status_code, status.HTTP_200_OK)
        self.assertEqual(self._salary(client, self.eng_manager).status_code, status.HTTP_200_OK)
        self.assertEqual(self._salary(client, self.cto).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self._salary(client, self.qa_manager).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_department_head_reaches_their_sub_departments(self):
        # Head of Technology, which contains Engineering and QA.
        self.tech.head = self.cto
        self.tech.save(update_fields=["head"])
        user = _user(
            "can_view_employees", "can_view_department_salary", employee=self.cto
        )
        client = _client(user)
        self.assertEqual(self._salary(client, self.senior_dev).status_code, status.HTTP_200_OK)
        self.assertEqual(self._salary(client, self.qa_manager).status_code, status.HTTP_200_OK)
        # HR is not under Technology.
        self.assertEqual(self._salary(client, self.hr_head).status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_sees_everybody(self):
        user = _user("can_view_employees", "can_view_all_salaries")
        client = _client(user)
        for employee in (self.ceo, self.cto, self.dev_one):
            self.assertEqual(self._salary(client, employee).status_code, status.HTTP_200_OK)

    def test_the_directory_hides_the_figures_it_may_not_show(self):
        user = _user(
            "can_view_employees",
            "can_view_own_salary",
            "can_view_subordinate_salary",
            employee=self.eng_manager,
        )
        client = _client(user)
        response = client.get(f"{BASE}/employees/", {"page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_code = {row["employee_code"]: row for row in response.data["results"]}
        # Below them: a figure. Above and beside them: None, which the screens
        # read as "not yours to see" rather than "no salary on record".
        self.assertIsNotNone(by_code["EMP004"]["salary"])
        self.assertIsNone(by_code["EMP002"]["salary"])
        self.assertIsNone(by_code["EMP007"]["salary"])

    def test_salary_filtering_needs_salary_access(self):
        user = _user("can_view_employees")
        client = _client(user)
        response = client.get(f"{BASE}/employees/", {"salary_min": "100000"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = client.get(f"{BASE}/employees/", {"sort": "salary"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_salary_filtering_stays_inside_the_viewers_reach(self):
        user = _user(
            "can_view_employees",
            "can_view_subordinate_salary",
            employee=self.eng_manager,
        )
        client = _client(user)
        response = client.get(f"{BASE}/employees/", {"salary_min": "1", "page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        codes = {row["employee_code"] for row in response.data["results"]}
        # Their own team only -- the CEO earns more than the filter's floor but
        # must not appear, not even as a count.
        self.assertEqual(codes, {"EMP004", "EMP005"})

    def test_reading_somebody_elses_salary_is_audited(self):
        user = _user("can_view_employees", "can_view_all_salaries")
        client = _client(user)
        self._salary(client, self.dev_one)
        self.assertTrue(
            EmployeeAuditLog.objects.filter(
                employee=self.dev_one, action="SALARY_VIEWED", performed_by=user
            ).exists()
        )

    def test_reading_your_own_salary_is_not_audited_as_a_lookup(self):
        user = _user("can_view_employees", "can_view_own_salary", employee=self.dev_one)
        client = _client(user)
        self._salary(client, self.dev_one)
        self.assertFalse(
            EmployeeAuditLog.objects.filter(
                employee=self.dev_one, action="SALARY_VIEWED"
            ).exists()
        )

    def test_history_needs_its_own_right(self):
        self._pay(self.senior_dev, 1200000, date(2026, 4, 1))
        user = _user(
            "can_view_employees", "can_view_subordinate_salary", employee=self.eng_manager
        )
        client = _client(user)
        response = self._salary(client, self.senior_dev)
        self.assertFalse(response.data["can_view_history"])
        # Only what they are paid now, not the years behind it.
        self.assertEqual(len(response.data["records"]), 1)

        with_history = _user(
            "can_view_employees",
            "can_view_subordinate_salary",
            "can_view_salary_history",
            employee=self.eng_manager,
        )
        response = self._salary(_client(with_history), self.senior_dev)
        self.assertTrue(response.data["can_view_history"])
        self.assertEqual(len(response.data["records"]), 2)


class ApiPermissionTests(OrgFixture):
    def test_the_module_is_closed_without_a_permission(self):
        client = _client(_user())
        self.assertEqual(
            client.get(f"{BASE}/employees/").status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_viewer_cannot_hire_or_move_anybody(self):
        client = _client(_user("can_view_employees"))
        self.assertEqual(client.get(f"{BASE}/employees/").status_code, status.HTTP_200_OK)
        self.assertEqual(
            client.post(f"{BASE}/employees/", {"employee_code": "X1", "first_name": "X"},
                        format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            client.post(
                f"{BASE}/employees/{self.dev_one.pk}/manager/",
                {"manager": self.qa_manager.pk},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_manager_change_through_the_api_moves_the_team_and_records_it(self):
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.post(
            f"{BASE}/employees/{self.senior_dev.pk}/manager/",
            {"manager": self.qa_manager.pk, "reason": "Team moved to QA"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["team_moved"], 2)
        audit = EmployeeAuditLog.objects.filter(
            employee=self.senior_dev, action="SUBTREE_MOVED"
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.reason, "Team moved to QA")

    def test_the_structural_fields_are_refused_on_the_plain_edit(self):
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.patch(
            f"{BASE}/employees/{self.dev_one.pk}/",
            {"reporting_manager": self.qa_manager.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reporting_manager", response.data)

    def test_another_companys_employee_is_not_reachable(self):
        outsider = self._hire("MART001", "Other", None, None, company=self.mart)
        client = _client(_user("can_view_employees"), company=OIL)
        response = client.get(f"{BASE}/employees/{outsider.pk}/reporting/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_proposing_a_revision_does_not_approve_it(self):
        client = _client(
            _user("can_view_employees", "can_view_all_salaries", "can_create_salary")
        )
        response = client.post(
            f"{BASE}/employees/{self.dev_one.pk}/salary/",
            {"basic_salary": "800000", "effective_from": "2026-04-01", "approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # `approve: true` is ignored without the approval right.
        self.assertEqual(response.data["status"], SalaryStatus.PENDING)

        approver = _client(
            _user(
                "can_view_employees", "can_view_all_salaries", "can_approve_salary_revision"
            )
        )
        decision = approver.post(
            f"{BASE}/salary-records/{response.data['id']}/approve/",
            {"reason": "Approved by finance"},
            format="json",
        )
        self.assertEqual(decision.status_code, status.HTTP_200_OK)
        self.assertIn(decision.data["status"], (SalaryStatus.ACTIVE, SalaryStatus.SCHEDULED))

    def test_the_reports_page_hides_money_without_salary_access(self):
        client = _client(_user("can_view_employees", "can_view_workforce_reports"))
        response = client.get(f"{BASE}/reports/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["salary"]["visible"])
        self.assertGreater(response.data["totals"]["headcount"], 0)

        with_money = _client(
            _user("can_view_employees", "can_view_workforce_reports", "can_view_all_salaries")
        )
        response = with_money.get(f"{BASE}/reports/")
        self.assertTrue(response.data["salary"]["visible"])
        self.assertEqual(response.data["salary"]["scope"], "all")

    def test_the_tree_comes_back_nested_with_subtree_sizes(self):
        client = _client(_user("can_view_employees"))
        response = client.get(f"{BASE}/tree/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["roots"]), 1)
        root = response.data["roots"][0]
        self.assertEqual(root["employee_code"], "EMP001")
        self.assertEqual(root["subtree_size"], 7)
        cto = next(child for child in root["children"] if child["employee_code"] == "EMP002")
        self.assertEqual(cto["subtree_size"], 5)

    def test_the_reporting_view_answers_every_question_at_once(self):
        client = _client(_user("can_view_employees"))
        response = client.get(f"{BASE}/employees/{self.senior_dev.pk}/reporting/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["manager"]["employee_code"], "EMP003")
        self.assertEqual(response.data["managers_manager"]["employee_code"], "EMP002")
        self.assertEqual(
            [person["employee_code"] for person in response.data["management_chain"]],
            ["EMP001", "EMP002", "EMP003"],
        )
        self.assertEqual(response.data["direct_report_count"], 2)
        self.assertEqual(response.data["department_path"], ["Technology", "Engineering"])


class SeedTests(APITestCase):
    def setUp(self):
        self.oil, self.mart = _companies()
        # The seed reports what it built; the tests do not need to read it.
        self.noise = StringIO()

    def test_the_seed_builds_the_org_from_the_brief(self):
        oil = self.oil
        call_command("seed_employee_hierarchy", "--company", OIL, stdout=self.noise)
        self.assertEqual(Employee.objects.filter(company=oil).count(), 15)

        ceo = Employee.objects.get(company=oil, employee_code="EMP001")
        self.assertIsNone(ceo.reporting_manager_id)
        self.assertEqual(ceo.hierarchy_level, 1)

        # The brief's own salary-history example: three records, one in force.
        self.assertEqual(ceo.salary_records.count(), 3)
        self.assertEqual(ceo.salary_records.filter(status=SalaryStatus.ACTIVE).count(), 1)

        developer = Employee.objects.get(company=oil, employee_code="EMP005")
        self.assertEqual(developer.hierarchy_level, 5)
        self.assertEqual(
            [person.employee_code for person in hierarchy.reporting_chain(developer)],
            ["EMP001", "EMP002", "EMP003", "EMP004"],
        )

    def test_the_seed_refuses_to_add_itself_twice(self):
        call_command("seed_employee_hierarchy", "--company", OIL, stdout=self.noise)
        with self.assertRaises(SystemExit):
            call_command("seed_employee_hierarchy", "--company", OIL, stdout=self.noise)


class LoginLinkTests(OrgFixture):
    """Linking an app login to an employee — what "own salary" hangs on.

    Without the link there is no such thing as "your own salary": the app knows
    a login, the directory knows a person, and nothing connects them. So the
    link has to be settable from the app, offered only for logins nobody else
    claims, and it has to take effect immediately — the employee should be able
    to open their own figure straight afterwards.
    """

    def test_the_meta_offers_only_unclaimed_logins(self):
        taken = _user("can_view_employees", employee=self.dev_one)
        free = _user("can_view_employees")
        client = _client(_user("can_view_employees", "can_manage_employees"))

        response = client.get(f"{BASE}/meta/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offered = {row["id"] for row in response.data["assignable_users"]}
        self.assertIn(free.id, offered)
        self.assertNotIn(taken.id, offered)

    def test_linking_a_login_lets_that_person_see_their_own_salary(self):
        self._pay(self.dev_one, 600000, date(2025, 4, 1))
        newcomer = _user("can_view_employees", "can_view_own_salary")

        # Before the link, their own salary is not "their own" to the system.
        their_client = _client(newcomer)
        self.assertEqual(
            their_client.get(f"{BASE}/employees/{self.dev_one.pk}/salary/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        hr = _client(_user("can_view_employees", "can_manage_employees"))
        linked = hr.patch(
            f"{BASE}/employees/{self.dev_one.pk}/",
            {"user": newcomer.id},
            format="json",
        )
        self.assertEqual(linked.status_code, status.HTTP_200_OK)
        self.assertEqual(linked.data["user"], newcomer.id)

        # And now it is.
        response = _client(newcomer).get(f"{BASE}/employees/{self.dev_one.pk}/salary/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["current"]["total_compensation"], "600000.00"
        )
        # Still nobody else's.
        self.assertEqual(
            _client(newcomer).get(f"{BASE}/employees/{self.senior_dev.pk}/salary/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_photo_can_be_uploaded_against_an_employee(self):
        """The directory takes a photo on edit, as multipart.

        Not on hire: creating somebody carries a nested joining salary, which
        multipart cannot express, so the photo is a second step against an
        employee who already exists.
        """
        # A one-pixel PNG is enough to prove the field accepts an upload.
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
            b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
            b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = SimpleUploadedFile("kavya.png", png, content_type="image/png")
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.patch(
            f"{BASE}/employees/{self.senior_dev.pk}/",
            {"photo": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["photo"])
        self.senior_dev.refresh_from_db()
        self.assertTrue(self.senior_dev.photo)


class PromotionPayloadTests(OrgFixture):
    """Promoting somebody the way the screen actually does it.

    The existing promotion tests called the service with the arguments a test
    finds convenient. The screen sends something slightly different — a salary
    block that carries its own ``reason`` — and that difference was the whole
    bug: the reason arrived twice at
    :func:`~employee_hierarchy.services.create_salary_record`, once from the
    promotion and once from inside the salary block, and Python refused the
    call. Every promotion with a revision attached, and every hire with a
    joining salary, failed with a 500.

    So these tests post the payloads the dialogs post, field for field.
    """

    def setUp(self):
        super().setUp()
        self.senior_rung = Designation.objects.create(
            company=self.oil, code="SR", name="Senior Developer", level=7
        )
        self.hr = _user(
            "can_view_employees",
            "can_manage_employees",
            "can_view_all_salaries",
            "can_create_salary",
            "can_update_salary",
            "can_approve_salary_revision",
        )
        self.client_hr = _client(self.hr)

    def test_promote_with_a_revision_the_way_the_dialog_sends_it(self):
        self._pay(self.dev_one, 600000, date(2025, 4, 1))

        response = self.client_hr.post(
            f"{BASE}/employees/{self.dev_one.pk}/promote/",
            {
                "designation": self.senior_rung.pk,
                "salary": {
                    "basic_salary": "700000",
                    "allowances": "100000",
                    "bonuses": "50000",
                    "effective_from": "2026-04-01",
                    # The dialog puts the promotion's reason inside the salary
                    # block as well as beside it. Both are legitimate.
                    "reason": "Promoted to Senior Developer",
                },
                "reason": "Promoted to Senior Developer",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["designation"])
        self.assertIsNotNone(response.data["salary_record_id"])

        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.designation_id, self.senior_rung.pk)
        self.assertEqual(self.dev_one.current_salary_amount, Decimal("850000.00"))

        # A promotion's revision is typed as a promotion, whatever else it says.
        record = EmployeeSalary.objects.get(pk=response.data["salary_record_id"])
        self.assertEqual(record.revision.revision_type, "PROMOTION")
        self.assertEqual(record.revision.reason, "Promoted to Senior Developer")
        self.assertEqual(record.revision.previous_amount, Decimal("600000.00"))

        # And the whole thing reads as one decision in the trail.
        self.assertTrue(
            EmployeeHistory.objects.filter(employee=self.dev_one, event="PROMOTED").exists()
        )

    def test_promote_with_a_revision_that_carries_its_own_reason(self):
        """The salary's own reason wins over the promotion's, when they differ."""
        self._pay(self.dev_one, 600000, date(2025, 4, 1))
        response = self.client_hr.post(
            f"{BASE}/employees/{self.dev_one.pk}/promote/",
            {
                "designation": self.senior_rung.pk,
                "salary": {
                    "basic_salary": "800000",
                    "effective_from": "2026-04-01",
                    "reason": "Band 4 minimum",
                    "revision_type": "ANNUAL_INCREMENT",
                },
                "reason": "Promoted to Senior Developer",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = EmployeeSalary.objects.get(pk=response.data["salary_record_id"])
        self.assertEqual(record.revision.reason, "Band 4 minimum")
        # The type the caller asked for is ignored: this endpoint promotes.
        self.assertEqual(record.revision.revision_type, "PROMOTION")

    def test_promote_without_a_revision_still_works(self):
        response = self.client_hr.post(
            f"{BASE}/employees/{self.dev_one.pk}/promote/",
            {"designation": self.senior_rung.pk, "reason": "Overdue"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["designation"])
        self.assertIsNone(response.data["salary_record_id"])

    def test_promote_moving_them_under_a_new_manager_at_the_same_time(self):
        response = self.client_hr.post(
            f"{BASE}/employees/{self.dev_one.pk}/promote/",
            {
                "designation": self.senior_rung.pk,
                "manager": self.cto.pk,
                "salary": {
                    "basic_salary": "900000",
                    "effective_from": "2026-04-01",
                    "reason": "Promotion",
                },
                "reason": "Taking over the platform team",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.reporting_manager_id, self.cto.pk)
        self.assertEqual(self.dev_one.designation_id, self.senior_rung.pk)

    def test_hiring_somebody_with_a_joining_salary_the_way_the_form_sends_it(self):
        response = self.client_hr.post(
            f"{BASE}/employees/",
            {
                "employee_code": "EMP100",
                "first_name": "Nikhil",
                "last_name": "Joshi",
                "email": "nikhil@example.com",
                "joining_date": "2026-09-01",
                "employment_status": "PROBATION",
                "department": self.engineering.pk,
                "designation": self.dev_rung.pk,
                "reporting_manager": self.eng_manager.pk,
                "job_title": "Developer",
                "initial_salary": {
                    "basic_salary": "480000",
                    "allowances": "120000",
                    "bonuses": "0",
                    "effective_from": "2026-09-01",
                    # The hire form sends this too.
                    "reason": "Joining salary",
                    "notes": "",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        hired = Employee.objects.get(company=self.oil, employee_code="EMP100")
        self.assertEqual(hired.reporting_manager_id, self.eng_manager.pk)
        record = hired.salary_records.get()
        self.assertEqual(record.total_compensation, Decimal("600000.00"))
        self.assertEqual(record.revision.revision_type, "INITIAL")
        self.assertEqual(record.revision.reason, "Joining salary")
        # Entered by somebody who may approve, so it is in force immediately.
        hired.refresh_from_db()
        self.assertEqual(hired.current_salary_amount, Decimal("600000.00"))


class StatusCountTests(OrgFixture):
    """The filter chips' counts.

    Pinned with **several** people per status on purpose. The original tests
    had exactly one employee in each state, which is the one shape that hides
    the bug this class exists for: the directory's counts are built on the
    filtered, sorted queryset ``apply_filters`` returns, and Django folds the
    columns of an explicit ordering into the GROUP BY of an aggregate. Every
    count came back as 1 — correct-looking on a fixture of one, and reading
    "Active 1" over a company of 252.
    """

    def test_counts_are_per_status_not_per_person(self):
        services.change_status(self.dev_one, EmploymentStatus.ON_LEAVE)
        services.change_status(self.dev_two, EmploymentStatus.ON_LEAVE)
        services.change_status(self.qa_manager, EmploymentStatus.PROBATION)

        client = _client(_user("can_view_employees"))
        response = client.get(f"{BASE}/employees/", {"include_past": "1", "page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        counts = response.data["status_counts"]
        self.assertEqual(counts["ON_LEAVE"], 2)
        self.assertEqual(counts["PROBATION"], 1)
        self.assertEqual(counts["ACTIVE"], 5)
        # The counts must add up to the unfiltered roster, or a chip is lying.
        self.assertEqual(sum(counts.values()), Employee.objects.filter(company=self.oil).count())

    def test_counts_ignore_the_status_filter_but_honour_the_others(self):
        """A chip says how many the *other* filters left in each state."""
        services.change_status(self.dev_one, EmploymentStatus.ON_LEAVE)
        client = _client(_user("can_view_employees"))

        # Narrowed to one department: the chips must count that department only.
        response = client.get(
            f"{BASE}/employees/",
            {"department": self.engineering.pk, "include_past": "1", "page_size": 100},
        )
        counts = response.data["status_counts"]
        self.assertEqual(counts["ON_LEAVE"], 1)
        self.assertEqual(counts["ACTIVE"], 3)

        # Asking for one status must not change what the chips report.
        response = client.get(
            f"{BASE}/employees/",
            {"department": self.engineering.pk, "status": "ACTIVE", "page_size": 100},
        )
        self.assertEqual(len(response.data["results"]), 3)
        self.assertEqual(response.data["status_counts"]["ON_LEAVE"], 1)

    def test_the_reports_page_counts_statuses_too(self):
        services.change_status(self.dev_one, EmploymentStatus.RESIGNED)
        client = _client(_user("can_view_employees", "can_view_workforce_reports"))
        response = client.get(f"{BASE}/reports/")
        by_status = {row["status"]: row["count"] for row in response.data["by_status"]}
        self.assertEqual(by_status["RESIGNED"], 1)
        self.assertEqual(by_status["ACTIVE"], 7)


class PermanentLabourTests(APITestCase):
    """The strength on the rolls, and how many of it turned up.

    Four things are worth holding, because each is a way the register could
    quietly lie: the strength is a snapshot per record (so a hire next month
    does not restate last week), a re-entered shift replaces rather than
    doubles, presence cannot be recorded before anybody says what the strength
    is, and writing a count needs its own grant while reading one does not.
    """

    def setUp(self):
        self.oil, self.mart = _companies()
        self.clerk = _user("can_view_employees", "can_record_labour_presence")
        self.hr = _user(
            "can_view_employees", "can_manage_org_structure", "can_record_labour_presence"
        )
        self.reader = _user("can_view_employees")
        # The plant-wide master the labour count is kept against: one list,
        # shared by every company, with the company carried by the row.
        self.production = OrgDepartment.objects.create(name="Production")
        self.packing = OrgDepartment.objects.create(name="Packing")
        self.store = OrgDepartment.objects.create(name="Store")

    def _set_strength(self, headcount, *, department=None, company=OIL):
        return _client(self.hr, company).put(
            f"{BASE}/labour-strength/",
            {"headcount": headcount, "department": department},
            format="json",
        )

    def _record(self, user, payload, *, company=OIL):
        return _client(user, company).post(
            f"{BASE}/labour-presence/", {"department": None, **payload}, format="json"
        )

    def test_strength_reads_as_unset_before_anybody_enters_it(self):
        response = _client(self.reader).get(f"{BASE}/labour-strength/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_set"])
        self.assertEqual(response.data["headcount"], 0)

    def test_setting_the_strength_needs_the_structure_right(self):
        refused = _client(self.clerk).put(
            f"{BASE}/labour-strength/", {"headcount": 85}, format="json"
        )
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._set_strength(85).status_code, status.HTTP_200_OK)
        self.assertEqual(
            PermanentLabourStrength.objects.get(
                company=self.oil, department=None
            ).headcount,
            85,
        )

    def test_recording_a_shift_snapshots_the_strength(self):
        self._set_strength(85)
        today = timezone.localdate()
        response = self._record(
            self.clerk, {"work_date": today, "shift": "DAY", "present_count": 78}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["strength"], 85)
        self.assertEqual(response.data["absent_count"], 7)

        # The plant hires its 86th. Last week still reads 78 of 85.
        self._set_strength(86)
        row = PermanentLabourPresence.objects.get(company=self.oil, work_date=today)
        self.assertEqual(row.strength, 85)

    def test_a_department_is_counted_against_its_own_strength(self):
        self._set_strength(40, department=self.production.id)
        self._set_strength(20, department=self.packing.id)
        today = timezone.localdate()
        response = self._record(
            self.clerk,
            {
                "department": self.production.id,
                "work_date": today,
                "shift": "DAY",
                "present_count": 36,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Production's 40, not the plant's 60 — a department measured against
        # the whole factory would read as permanently short.
        self.assertEqual(response.data["strength"], 40)
        self.assertEqual(response.data["department_name"], "Production")

    def test_the_same_shift_in_two_departments_is_two_records(self):
        self._set_strength(40, department=self.production.id)
        self._set_strength(20, department=self.packing.id)
        today = timezone.localdate()
        for department, present in ((self.production, 36), (self.packing, 18)):
            self._record(
                self.clerk,
                {
                    "department": department.id,
                    "work_date": today,
                    "shift": "DAY",
                    "present_count": present,
                },
            )
        self.assertEqual(
            PermanentLabourPresence.objects.filter(
                company=self.oil, work_date=today, shift="DAY"
            ).count(),
            2,
        )

    def test_no_department_asked_for_reads_as_the_plant_total(self):
        self._set_strength(40, department=self.production.id)
        self._set_strength(20, department=self.packing.id)
        today = timezone.localdate()
        self._record(
            self.clerk,
            {
                "department": self.production.id,
                "work_date": today,
                "shift": "DAY",
                "present_count": 36,
            },
        )
        self._record(
            self.clerk,
            {
                "department": self.packing.id,
                "work_date": today,
                "shift": "DAY",
                "present_count": 18,
            },
        )

        strength = _client(self.hr).get(f"{BASE}/labour-strength/")
        self.assertEqual(strength.data["headcount"], 60)
        self.assertEqual(strength.data["scope"], "ALL")
        self.assertEqual(
            [row["department_name"] for row in strength.data["departments"]],
            ["Packing", "Production"],
        )

        presence = _client(self.reader).get(f"{BASE}/labour-presence/")
        self.assertEqual(len(presence.data["results"]), 1)
        total = presence.data["results"][0]
        self.assertEqual(total["present_count"], 54)
        self.assertEqual(total["strength"], 60)
        self.assertEqual(total["departments_counted"], 2)
        # A total is not something a person can take a headcount of, so the
        # screen must not offer it for correction.
        self.assertFalse(total["is_editable"])
        self.assertIsNone(total["id"])

    def test_one_department_asked_for_reads_only_its_own(self):
        self._set_strength(40, department=self.production.id)
        self._set_strength(20, department=self.packing.id)
        today = timezone.localdate()
        self._record(
            self.clerk,
            {
                "department": self.production.id,
                "work_date": today,
                "shift": "DAY",
                "present_count": 36,
            },
        )
        self._record(
            self.clerk,
            {
                "department": self.packing.id,
                "work_date": today,
                "shift": "DAY",
                "present_count": 18,
            },
        )

        scoped = _client(self.reader).get(
            f"{BASE}/labour-presence/?department={self.production.id}"
        )
        self.assertEqual([row["present_count"] for row in scoped.data["results"]], [36])
        self.assertEqual(scoped.data["strength"]["headcount"], 40)
        self.assertTrue(scoped.data["results"][0]["is_editable"])

    def test_the_undivided_bucket_is_not_the_same_as_no_department_asked_for(self):
        """``?department=none`` is its own row, not "give me everything"."""
        self._set_strength(85)
        self._set_strength(40, department=self.production.id)
        undivided = _client(self.hr).get(f"{BASE}/labour-strength/?department=none")
        self.assertEqual(undivided.data["headcount"], 85)
        self.assertEqual(undivided.data["scope"], "UNDIVIDED")
        everything = _client(self.hr).get(f"{BASE}/labour-strength/")
        self.assertEqual(everything.data["headcount"], 125)

    def test_one_shared_department_holds_a_figure_per_company(self):
        """The department list is the plant's; the headcount is the company's.

        Oil and Mart both have a Production, and it is the same row in the
        master. Their strengths must not become each other's.
        """
        self._set_strength(40, department=self.production.id)
        self._set_strength(12, department=self.production.id, company=MART)

        oil = _client(self.hr).get(f"{BASE}/labour-strength/?department={self.production.id}")
        mart = _client(self.hr, MART).get(
            f"{BASE}/labour-strength/?department={self.production.id}"
        )
        self.assertEqual(oil.data["headcount"], 40)
        self.assertEqual(mart.data["headcount"], 12)
        self.assertEqual(
            PermanentLabourStrength.objects.filter(department=self.production).count(), 2
        )

    def test_a_department_that_does_not_exist_is_a_404_to_read(self):
        response = _client(self.reader).get(f"{BASE}/labour-presence/?department=999999")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_same_shift_twice_corrects_rather_than_doubles(self):
        self._set_strength(85)
        today = timezone.localdate()
        self._record(self.clerk, {"work_date": today, "shift": "DAY", "present_count": 78})
        again = self._record(
            self.clerk,
            {"work_date": today, "shift": "DAY", "present_count": 80, "remark": "recount"},
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        rows = PermanentLabourPresence.objects.filter(company=self.oil, work_date=today)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().present_count, 80)
        self.assertEqual(rows.first().remark, "recount")

    def test_day_and_night_are_separate_records(self):
        self._set_strength(85)
        today = timezone.localdate()
        self._record(self.clerk, {"work_date": today, "shift": "DAY", "present_count": 62})
        self._record(self.clerk, {"work_date": today, "shift": "NIGHT", "present_count": 16})
        self.assertEqual(
            PermanentLabourPresence.objects.filter(company=self.oil, work_date=today).count(), 2
        )

    def test_presence_is_refused_until_the_strength_is_set(self):
        response = self._record(
            self.clerk,
            {"work_date": timezone.localdate(), "shift": "DAY", "present_count": 78},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PermanentLabourPresence.objects.exists())

    def test_a_department_without_a_strength_is_refused_even_if_others_have_one(self):
        self._set_strength(40, department=self.production.id)
        refused = self._record(
            self.clerk,
            {
                "department": self.packing.id,
                "work_date": timezone.localdate(),
                "shift": "DAY",
                "present_count": 18,
            },
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Packing", str(refused.data))

    def test_a_future_shift_is_refused(self):
        self._set_strength(85)
        response = self._record(
            self.clerk,
            {
                "work_date": timezone.localdate() + timedelta(days=1),
                "shift": "DAY",
                "present_count": 78,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recording_needs_its_own_grant_but_reading_does_not(self):
        self._set_strength(85)
        refused = self._record(
            self.reader,
            {"work_date": timezone.localdate(), "shift": "DAY", "present_count": 78},
        )
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        readable = _client(self.reader).get(f"{BASE}/labour-presence/")
        self.assertEqual(readable.status_code, status.HTTP_200_OK)
        self.assertEqual(readable.data["results"], [])

    def test_the_window_defaults_to_a_fortnight_and_is_company_scoped(self):
        self._set_strength(85)
        self._set_strength(40, company=MART)
        today = timezone.localdate()
        self._record(self.clerk, {"work_date": today, "shift": "DAY", "present_count": 78})
        self._record(
            self.clerk, {"work_date": today - timedelta(days=20), "shift": "DAY", "present_count": 70}
        )
        self._record(
            self.clerk, {"work_date": today, "shift": "DAY", "present_count": 31}, company=MART
        )

        oil = _client(self.reader).get(f"{BASE}/labour-presence/")
        self.assertEqual([row["present_count"] for row in oil.data["results"]], [78])
        self.assertEqual(oil.data["strength"]["headcount"], 85)

        widened = _client(self.reader).get(
            f"{BASE}/labour-presence/?from={today - timedelta(days=30)}"
        )
        self.assertEqual([row["present_count"] for row in widened.data["results"]], [78, 70])

        mart = _client(self.reader, MART).get(f"{BASE}/labour-presence/")
        self.assertEqual([row["present_count"] for row in mart.data["results"]], [31])

    def test_a_count_above_the_strength_is_kept_and_flagged(self):
        self._set_strength(85)
        response = self._record(
            self.clerk,
            {"work_date": timezone.localdate(), "shift": "DAY", "present_count": 88},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_over_strength"])
        self.assertEqual(response.data["absent_count"], 0)


class PermanentLabourAuditTests(APITestCase):
    """The trail behind the two figures.

    Both are overwritten in place — the strength is one row, and re-recording a
    shift replaces its count — so without a trail the question "it said 78, who
    made it 63?" has no answer at all. These hold that every write leaves a row,
    that the row carries what the figure *was*, and that a trail cannot be read
    across companies.
    """

    def setUp(self):
        self.oil, self.mart = _companies()
        self.hr = _user(
            "can_view_employees", "can_manage_org_structure", "can_record_labour_presence"
        )
        self.today = timezone.localdate()
        self.production = OrgDepartment.objects.create(name="Production")

    def _set_strength(self, headcount, *, note="", department=None, company=OIL):
        return _client(self.hr, company).put(
            f"{BASE}/labour-strength/",
            {"headcount": headcount, "note": note, "department": department},
            format="json",
        )

    def _record(self, present, *, remark="", department=None, company=OIL):
        return _client(self.hr, company).post(
            f"{BASE}/labour-presence/",
            {
                "department": department,
                "work_date": self.today,
                "shift": "DAY",
                "present_count": present,
                "remark": remark,
            },
            format="json",
        )

    def test_the_first_strength_and_every_change_leave_a_row(self):
        self._set_strength(85, note="sanctioned")
        self._set_strength(86)

        trail = _client(self.hr).get(f"{BASE}/labour-strength/audit/")
        self.assertEqual(trail.status_code, status.HTTP_200_OK)
        rows = trail.data["results"]
        self.assertEqual(len(rows), 2)
        # Newest first: the change, then the row that created the figure.
        self.assertEqual((rows[0]["previous_count"], rows[0]["new_count"]), (85, 86))
        self.assertFalse(rows[0]["is_first"])
        self.assertIsNone(rows[1]["previous_count"])
        self.assertTrue(rows[1]["is_first"])
        self.assertEqual(rows[1]["new_remark"], "sanctioned")
        self.assertEqual(rows[0]["performed_by_detail"]["full_name"], self.hr.full_name)

    def test_a_corrected_count_keeps_the_figure_it_replaced(self):
        self._set_strength(85)
        created = self._record(78)
        self._record(63, remark="recount")

        presence_id = created.data["id"]
        trail = _client(self.hr).get(f"{BASE}/labour-presence/{presence_id}/audit/")
        rows = trail.data["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["previous_count"], rows[0]["new_count"]), (78, 63))
        self.assertEqual(rows[0]["new_remark"], "recount")
        self.assertEqual(rows[0]["strength"], 85)
        self.assertIsNone(rows[1]["previous_count"])
        # The row itself was overwritten; only the trail still holds the 78.
        self.assertEqual(
            PermanentLabourPresence.objects.get(pk=presence_id).present_count, 63
        )

    def test_the_strength_trail_does_not_cross_companies(self):
        self._set_strength(85)
        self._set_strength(40, company=MART)

        oil = _client(self.hr).get(f"{BASE}/labour-strength/audit/")
        self.assertEqual([row["new_count"] for row in oil.data["results"]], [85])
        mart = _client(self.hr, MART).get(f"{BASE}/labour-strength/audit/")
        self.assertEqual([row["new_count"] for row in mart.data["results"]], [40])

    def test_another_companys_shift_is_a_404_not_a_peek(self):
        self._set_strength(40, company=MART)
        mart_row = self._record(31, company=MART)
        refused = _client(self.hr).get(f"{BASE}/labour-presence/{mart_row.data['id']}/audit/")
        self.assertEqual(refused.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_refused_write_leaves_no_trail(self):
        # No strength yet, so the count is refused — and must not be audited as
        # though it happened.
        refused = self._record(78)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PermanentLabourAudit.objects.exists())


class BranchImportTests(OrgFixture):
    """Reading the branch off the workbook's SAP column.

    The three things worth pinning down are the three that would be wrong
    *silently*: two spellings of one branch becoming two branches, a name match
    overriding an exact code match, and one code typed against two people being
    resolved by whichever row happened to be last. Each of those files somebody
    under a branch meant for somebody else, and none of them raises anything.
    """

    def setUp(self):
        super().setUp()
        self.oil_branch = Branch.objects.create(
            company=self.oil, code="JIVO_OIL", name="Jivo Oil", is_default=True
        )
        Employee.objects.filter(company=self.oil).update(branch=self.oil_branch)

    def _workbook(self, rows):
        import openpyxl

        path = Path(tempfile.gettempdir()) / f"branches-{uuid4().hex}.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet.append(["S.NO", "Management", "JWPL", "Name", "Department",
                      "Designation", "Sub Department", "SAP", "HOD"])
        for n, (code, name, sap) in enumerate(rows, start=1):
            sheet.append([n, "", code, name, "", "", "", sap, ""])
        book.save(path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def _run(self, rows, *args):
        out, err = StringIO(), StringIO()
        call_command(
            "import_employee_branches",
            "--file", self._workbook(rows),
            "--company", OIL,
            *args,
            stdout=out, stderr=err,
        )
        return out.getvalue() + err.getvalue()

    def test_two_spellings_of_one_branch_make_one_branch(self):
        """'BEV' and 'Bev' are the same place; two rows would split every report."""
        self._run(
            [("EMP001", "Arun Test", "BEV"), ("EMP002", "Priya Test", "Bev")],
            "--commit",
        )
        self.assertEqual(Branch.objects.filter(company=self.oil, name__iexact="bev").count(), 1)
        self.ceo.refresh_from_db()
        self.cto.refresh_from_db()
        self.assertEqual(self.ceo.branch, self.cto.branch)

    def test_an_employee_is_filed_under_the_sheets_branch(self):
        self._run([("EMP001", "Arun Test", "Water")], "--commit")
        self.ceo.refresh_from_db()
        self.assertEqual(self.ceo.branch.name, "Water")

    def test_a_dry_run_writes_nothing(self):
        output = self._run([("EMP001", "Arun Test", "Water")])
        self.assertIn("Dry run", output)
        self.assertFalse(Branch.objects.filter(name="Water").exists())
        self.ceo.refresh_from_db()
        self.assertEqual(self.ceo.branch, self.oil_branch)

    def test_a_name_match_never_overrides_a_code_match(self):
        """A code is exact; a name is a guess that happened to be unique."""
        self._run(
            [("EMP001", "Arun Test", "Mart"), ("", "Arun Test", "Construction")],
            "--match-by-name",
            "--commit",
        )
        self.ceo.refresh_from_db()
        self.assertEqual(self.ceo.branch.name, "Mart")

    def test_one_code_against_two_people_is_refused_not_guessed(self):
        output = self._run(
            [("EMP001", "Arun Test", "Oil"), ("EMP001", "Someone Else", "Bev")],
            "--commit",
        )
        self.assertIn("CONFLICT", output)
        self.ceo.refresh_from_db()
        # Left exactly as it was, rather than taking whichever row came last.
        self.assertEqual(self.ceo.branch, self.oil_branch)

    def test_somebody_the_sheet_omits_is_left_alone(self):
        self._run([("EMP001", "Arun Test", "Oil")], "--commit")
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.branch, self.oil_branch)

    def test_the_segment_places_people_the_sheet_forgot(self):
        self.dev_one.sap_segment = "Water"
        self.dev_one.save(update_fields=["sap_segment"])
        self._run([("EMP001", "Arun Test", "Oil")], "--fill-from-sap-segment", "--commit")
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.branch.name, "Water")

    def test_dropping_unused_clears_people_rather_than_inventing_a_branch(self):
        """The sheet not naming somebody says nothing about where they work."""
        self._run(
            [("EMP001", "Arun Test", "Oil")],
            "--default", "OIL",
            "--drop-unused",
            "--commit",
        )
        self.assertFalse(Branch.objects.filter(name="Jivo Oil").exists())
        self.ceo.refresh_from_db()
        self.dev_one.refresh_from_db()
        self.assertEqual(self.ceo.branch.name, "Oil")
        self.assertIsNone(self.dev_one.branch)

    def test_the_named_default_is_the_one_in_force_afterwards(self):
        self._run(
            [("EMP001", "Arun Test", "Oil"), ("EMP002", "Priya Test", "Mart")],
            "--default", "MART",
            "--commit",
        )
        self.assertEqual(
            Branch.objects.get(company=self.oil, is_default=True).name, "Mart"
        )

    def test_a_missing_sap_column_is_refused_outright(self):
        import openpyxl

        path = Path(tempfile.gettempdir()) / f"bad-{uuid4().hex}.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Sheet1"
        book.active.append(["S.NO", "JWPL", "Name"])
        book.save(path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        with self.assertRaises(CommandError):
            call_command(
                "import_employee_branches", "--file", str(path), "--company", OIL,
                stdout=StringIO(), stderr=StringIO(),
            )


class BranchTests(OrgFixture):
    """The branch master, and the one invariant it has: a single default.

    Branch is deliberately a label -- it scopes nothing and filters nothing --
    so what is worth pinning down is not who it lets see what, but that the
    default answers exactly once. Two defaults, or none, and the hire form
    either picks arbitrarily or picks nothing, and neither failure announces
    itself.
    """

    def setUp(self):
        super().setUp()
        self.oil_branch = Branch.objects.create(
            company=self.oil, code="OIL", name="Jivo Oil", is_default=True
        )
        self.mart_branch = Branch.objects.create(
            company=self.oil, code="MART", name="Jivo Mart"
        )

    def _manager(self):
        return _client(_user("can_view_employees", "can_manage_org_structure"))

    # -- the master ---------------------------------------------------------

    def test_a_viewer_reads_the_master_but_cannot_change_it(self):
        client = _client(_user("can_view_employees"))
        self.assertEqual(client.get(f"{BASE}/branches/").status_code, status.HTTP_200_OK)
        self.assertEqual(
            client.post(
                f"{BASE}/branches/", {"code": "NEW", "name": "New"}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_the_module_is_closed_to_somebody_with_no_permission(self):
        client = _client(_user())
        self.assertEqual(
            client.get(f"{BASE}/branches/").status_code, status.HTTP_403_FORBIDDEN
        )

    def test_another_companys_branches_are_not_listed(self):
        Branch.objects.create(company=self.mart, code="X", name="Mart Only")
        response = self._manager().get(f"{BASE}/branches/")
        names = {row["name"] for row in response.data["results"]}
        self.assertEqual(names, {"Jivo Oil", "Jivo Mart"})

    def test_the_first_branch_in_a_company_becomes_its_default_unasked(self):
        """A master with rows but no default leaves the hire form with nothing."""
        client = _client(
            _user("can_view_employees", "can_manage_org_structure"), company=MART
        )
        response = client.post(
            f"{BASE}/branches/", {"code": "FIRST", "name": "First"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_default"])

    def test_a_later_branch_does_not_steal_the_default(self):
        response = self._manager().post(
            f"{BASE}/branches/", {"code": "THIRD", "name": "Third"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["is_default"])
        self.oil_branch.refresh_from_db()
        self.assertTrue(self.oil_branch.is_default)

    def test_a_duplicate_code_is_refused_with_a_message_not_a_500(self):
        """The database would catch it; a 500 is not something HR can act on."""
        response = self._manager().post(
            f"{BASE}/branches/", {"code": "oil", "name": "Another Oil"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("code", response.data)

    def test_a_duplicate_name_is_refused_the_same_way(self):
        response = self._manager().post(
            f"{BASE}/branches/", {"code": "OIL2", "name": "jivo oil"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_a_branch_may_keep_its_own_code_on_an_edit(self):
        response = self._manager().patch(
            f"{BASE}/branches/{self.oil_branch.pk}/",
            {"code": "OIL", "description": "Renamed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["description"], "Renamed")

    def test_the_same_code_is_free_in_another_company(self):
        client = _client(
            _user("can_view_employees", "can_manage_org_structure"), company=MART
        )
        response = client.post(
            f"{BASE}/branches/", {"code": "OIL", "name": "Jivo Oil"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # -- the default --------------------------------------------------------

    def test_promoting_a_branch_demotes_the_one_that_held_it(self):
        response = self._manager().patch(
            f"{BASE}/branches/{self.mart_branch.pk}/", {"is_default": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_default"])
        self.oil_branch.refresh_from_db()
        self.assertFalse(self.oil_branch.is_default)
        self.assertEqual(
            Branch.objects.filter(company=self.oil, is_default=True).count(), 1
        )

    def test_the_default_cannot_simply_be_cleared(self):
        response = self._manager().patch(
            f"{BASE}/branches/{self.oil_branch.pk}/", {"is_default": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.oil_branch.refresh_from_db()
        self.assertTrue(self.oil_branch.is_default)

    def test_the_default_cannot_be_retired_or_deactivated(self):
        client = self._manager()
        self.assertEqual(
            client.delete(f"{BASE}/branches/{self.oil_branch.pk}/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            client.patch(
                f"{BASE}/branches/{self.oil_branch.pk}/",
                {"status": "INACTIVE"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.oil_branch.refresh_from_db()
        self.assertEqual(self.oil_branch.status, "ACTIVE")

    def test_a_non_default_branch_retires_and_keeps_its_people(self):
        self.dev_one.branch = self.mart_branch
        self.dev_one.save(update_fields=["branch"])
        response = self._manager().delete(f"{BASE}/branches/{self.mart_branch.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mart_branch.refresh_from_db()
        self.assertEqual(self.mart_branch.status, "INACTIVE")
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.branch_id, self.mart_branch.pk)

    def test_a_retired_branch_cannot_be_made_the_default(self):
        self.mart_branch.status = "INACTIVE"
        self.mart_branch.save(update_fields=["status"])
        response = self._manager().patch(
            f"{BASE}/branches/{self.mart_branch.pk}/", {"is_default": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.oil_branch.refresh_from_db()
        self.assertTrue(self.oil_branch.is_default)

    # -- the employee side --------------------------------------------------

    def test_a_new_employee_lands_on_the_default_without_being_told(self):
        hire = services.create_employee(
            company=self.oil,
            data={
                "employee_code": "EMP900",
                "first_name": "Unbranched",
                "joining_date": date(2026, 1, 1),
            },
        )
        self.assertEqual(hire.branch_id, self.oil_branch.pk)

    def test_a_chosen_branch_wins_over_the_default(self):
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.post(
            f"{BASE}/employees/",
            {
                "employee_code": "EMP901",
                "first_name": "Chosen",
                "branch": self.mart_branch.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["branch"], self.mart_branch.pk)

    def test_a_retired_branch_is_not_offered_to_a_new_hire(self):
        self.mart_branch.status = "INACTIVE"
        self.mart_branch.save(update_fields=["status"])
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.post(
            f"{BASE}/employees/",
            {
                "employee_code": "EMP902",
                "first_name": "Refused",
                "branch": self.mart_branch.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("branch", response.data)

    def test_another_companys_branch_cannot_be_pinned_on_an_employee(self):
        outsider_branch = Branch.objects.create(
            company=self.mart, code="OUT", name="Outside"
        )
        client = _client(_user("can_view_employees", "can_manage_employees"), company=OIL)
        response = client.post(
            f"{BASE}/employees/",
            {
                "employee_code": "EMP903",
                "first_name": "Crosser",
                "branch": outsider_branch.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_changing_a_branch_is_recorded_like_any_other_plain_edit(self):
        client = _client(_user("can_view_employees", "can_manage_employees"))
        response = client.patch(
            f"{BASE}/employees/{self.dev_one.pk}/",
            {"branch": self.mart_branch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.branch_id, self.mart_branch.pk)
        audit = EmployeeAuditLog.objects.filter(
            employee=self.dev_one, field="branch"
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.new_value, "Jivo Mart")

    # -- the backfill for people who predate the field -----------------------

    def test_the_backfill_is_a_dry_run_until_told_otherwise(self):
        out = StringIO()
        call_command("backfill_employee_branches", stdout=out, stderr=StringIO())
        self.assertIn("Dry run", out.getvalue())
        self.assertFalse(Employee.objects.filter(branch__isnull=False).exists())

    def test_the_backfill_gives_unbranched_people_the_default(self):
        call_command(
            "backfill_employee_branches", "--commit", stdout=StringIO(), stderr=StringIO()
        )
        self.assertFalse(
            Employee.objects.filter(company=self.oil, branch__isnull=True).exists()
        )
        self.assertEqual(
            Employee.objects.filter(company=self.oil, branch=self.oil_branch).count(),
            Employee.objects.filter(company=self.oil).count(),
        )

    def test_the_backfill_never_overwrites_a_branch_somebody_chose(self):
        self.dev_one.branch = self.mart_branch
        self.dev_one.save(update_fields=["branch"])
        call_command(
            "backfill_employee_branches", "--commit", stdout=StringIO(), stderr=StringIO()
        )
        self.dev_one.refresh_from_db()
        self.assertEqual(self.dev_one.branch_id, self.mart_branch.pk)

    def test_the_backfill_can_be_pointed_at_another_branch(self):
        call_command(
            "backfill_employee_branches",
            "--branch",
            "MART",
            "--commit",
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.ceo.refresh_from_db()
        self.assertEqual(self.ceo.branch_id, self.mart_branch.pk)

    def test_the_backfill_refuses_to_guess_when_there_is_no_default(self):
        """Which branch these people belong to is the one thing it must not invent."""
        Branch.objects.filter(company=self.oil).update(is_default=False)
        err = StringIO()
        call_command("backfill_employee_branches", "--commit", stdout=StringIO(), stderr=err)
        self.assertIn("no default branch set", err.getvalue())
        self.assertFalse(Employee.objects.filter(branch__isnull=False).exists())

    # -- what the form is handed --------------------------------------------

    def test_meta_carries_the_master_and_names_the_default(self):
        response = self._manager().get(f"{BASE}/meta/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["default_branch"], self.oil_branch.pk)
        names = {row["name"] for row in response.data["branches"]}
        self.assertEqual(names, {"Jivo Oil", "Jivo Mart"})

    def test_meta_reports_no_default_rather_than_guessing_one(self):
        Branch.objects.filter(company=self.oil).delete()
        response = self._manager().get(f"{BASE}/meta/")
        self.assertIsNone(response.data["default_branch"])
        self.assertEqual(response.data["branches"], [])
