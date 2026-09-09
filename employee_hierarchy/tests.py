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

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from . import hierarchy, services
from .constants import EmploymentStatus, SalaryStatus
from .models import (
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
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
