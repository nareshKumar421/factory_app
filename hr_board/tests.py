"""
hr_board/tests.py

What these pin down, and why each one is here rather than being obvious.

The centre of gravity is :class:`LabourIntakeTests`. The labour gate books
every labourer twice -- once at the gate and once when an HOD allocates them to
a department -- so the naive ``Sum(count_in)`` is exactly double the number of
people who walked in. That bug is invisible: the board would read high,
plausibly, forever, and the figure is the one the board exists to show. It is
tested against a day built to look exactly like a real one.

The rest guard decisions that a later reader would otherwise "tidy up":
head count is group-wide on purpose, the trend keeps its zeros on purpose, and
both shifts are always present on purpose.
"""

from datetime import date, timedelta

from django.test import TestCase

from accounts.models import Department as PlantDepartment
from company.models import Company
from employee_hierarchy.constants import EmploymentStatus
from employee_hierarchy.models import Department as HrDepartment, Employee
from labour_gate.models import LabourGateEntry
from person_gatein.models import Contractor

from .constants import HR_BOARD_TREND_DAYS
from .services import HrBoardService

TODAY = date(2026, 9, 16)


class HrBoardTestCase(TestCase):
    """Shared fixtures: two companies, one directory, one gate."""

    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        cls.production = PlantDepartment.objects.create(name="production(oil)")
        cls.mess = PlantDepartment.objects.create(name="Mess")
        cls.contractor = Contractor.objects.create(contractor_name="Imran")
        cls.other_contractor = Contractor.objects.create(contractor_name="Nayeem")

    def board(self, company_code="JIVO_OIL", today=TODAY):
        return HrBoardService(company_code=company_code, today=today).build()

    def intake(self, *, count, day=TODAY, shift="DAY", company=None, contractor=None):
        """A gate-intake row: no department, because nobody has been placed yet."""
        return LabourGateEntry.objects.create(
            company=company or self.oil,
            department=None,
            contractor=contractor or self.contractor,
            work_date=day,
            shift=shift,
            count_in=count,
        )

    def allocation(self, *, count, department, day=TODAY, shift="DAY", contractor=None):
        """An HOD's placement of those same people into a department."""
        return LabourGateEntry.objects.create(
            company=self.oil,
            department=department,
            contractor=contractor or self.contractor,
            work_date=day,
            shift=shift,
            count_in=count,
        )


class LabourIntakeTests(HrBoardTestCase):
    """The double-count trap, and the day-shape around it."""

    def test_a_fully_allocated_day_is_not_counted_twice(self):
        """THE test. 30 people came in; the board must say 30, not 60.

        Built exactly like a real day: the gate books 30, then the HOD places
        all 30 across two departments, which doubles ``Sum(count_in)`` over the
        table without another soul entering the factory.
        """
        self.intake(count=30)
        self.allocation(count=18, department=self.production)
        self.allocation(count=12, department=self.mess)

        labour = self.board()["labour"]

        self.assertEqual(labour["today_in"], 30)
        self.assertEqual(labour["today_allocated"], 30)
        self.assertEqual(labour["pending_allocation"], 0)

    def test_partial_allocation_is_reported_not_hidden(self):
        """Mid-morning: 30 in, 18 placed. The gap is a figure, not an error."""
        self.intake(count=30)
        self.allocation(count=18, department=self.production)

        labour = self.board()["labour"]

        self.assertEqual(labour["today_in"], 30)
        self.assertEqual(labour["today_allocated"], 18)
        self.assertEqual(labour["pending_allocation"], 12)

    def test_departments_come_from_the_allocation_rows(self):
        """The intake rows cannot answer "where did they work" -- only these can."""
        self.intake(count=30)
        self.allocation(count=18, department=self.production)
        self.allocation(count=12, department=self.mess)

        rows = {row["label"]: row["count"] for row in self.board()["labour"]["departments"]["rows"]}

        self.assertEqual(rows, {"production(oil)": 18, "Mess": 12})

    def test_contractors_come_from_the_intake_rows(self):
        """Counting contractors off the allocation rows would double them too."""
        self.intake(count=20, contractor=self.contractor)
        self.intake(count=10, contractor=self.other_contractor)
        self.allocation(count=20, department=self.production, contractor=self.contractor)

        labour = self.board()["labour"]
        rows = {row["label"]: row["count"] for row in labour["contractors"]["rows"]}

        self.assertEqual(rows, {"Imran": 20, "Nayeem": 10})
        self.assertEqual(labour["contractor_count"], 2)

    def test_a_soft_deleted_row_is_not_counted(self):
        """``deleted_at`` is the gate's undo. A deleted intake never happened."""
        self.intake(count=30)
        killed = self.intake(count=5, contractor=self.other_contractor)
        killed.deleted_at = "2026-09-16T10:00:00Z"
        killed.save(update_fields=["deleted_at"])

        self.assertEqual(self.board()["labour"]["today_in"], 30)

    def test_night_and_day_are_both_present_even_at_zero(self):
        """A tile whose shape changes through the day is one people stop reading."""
        self.intake(count=30, shift="DAY")

        shifts = self.board()["labour"]["shifts"]

        self.assertEqual([s["key"] for s in shifts], ["DAY", "NIGHT"])
        self.assertEqual([s["count"] for s in shifts], [30, 0])

    def test_labour_follows_the_company_switcher(self):
        """Unlike head count. The gate really does book the two plants apart."""
        self.intake(count=30, company=self.oil)
        self.intake(count=4, company=self.bev)

        self.assertEqual(self.board("JIVO_OIL")["labour"]["today_in"], 30)
        self.assertEqual(self.board("JIVO_BEVERAGES")["labour"]["today_in"], 4)


class LabourTrendTests(HrBoardTestCase):
    """The window, its zeros, and the average that ignores them."""

    def test_every_day_in_the_window_is_present(self):
        self.intake(count=30)

        trend = self.board()["labour"]["trend"]

        self.assertEqual(len(trend), HR_BOARD_TREND_DAYS)
        self.assertEqual(trend[-1], {"date": TODAY.isoformat(), "count": 30})
        self.assertEqual(trend[0]["count"], 0)

    def test_a_closed_day_stays_in_the_series_as_zero(self):
        """Closing the gap would draw a straight line through a shutdown."""
        self.intake(count=30, day=TODAY)
        self.intake(count=20, day=TODAY - timedelta(days=2))

        counts = {row["date"]: row["count"] for row in self.board()["labour"]["trend"]}

        self.assertEqual(counts[(TODAY - timedelta(days=1)).isoformat()], 0)

    def test_the_average_ignores_days_nobody_worked(self):
        """Otherwise a month of Sundays reports an average no day looked like."""
        self.intake(count=30, day=TODAY)
        self.intake(count=20, day=TODAY - timedelta(days=1))

        labour = self.board()["labour"]

        self.assertEqual(labour["working_days"], 2)
        self.assertEqual(labour["average_per_working_day"], 25.0)

    def test_a_future_dated_row_is_never_read(self):
        """A row dated forward is a typo, and would draw a real-looking bar."""
        self.intake(count=30, day=TODAY)
        self.intake(count=999, day=TODAY + timedelta(days=3))

        labour = self.board()["labour"]

        self.assertEqual(labour["today_in"], 30)
        self.assertNotIn(999, [row["count"] for row in labour["trend"]])

    def test_an_empty_gate_says_so_rather_than_dividing_by_zero(self):
        labour = self.board()["labour"]

        self.assertEqual(labour["today_in"], 0)
        self.assertIsNone(labour["average_per_working_day"])
        self.assertEqual(labour["working_days"], 0)


class HeadcountTests(HrBoardTestCase):
    """Group-wide on purpose, and in-service rather than merely active."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.canola = HrDepartment.objects.create(
            company=cls.oil, code="CAN", name="Canola (Production)"
        )

    def employee(self, code, *, segment="Oil", status=EmploymentStatus.ACTIVE, department=None):
        return Employee.objects.create(
            company=self.oil,
            employee_code=code,
            first_name=code,
            sap_segment=segment,
            employment_status=status,
            department=department,
        )

    def test_headcount_is_group_wide_not_company_scoped(self):
        """The directory sits under one company; the segment is the real split.

        Filtering by the switcher would report the whole factory under Oil and
        zero under Beverages -- not a smaller truth, a false one.
        """
        self.employee("E1", segment="Oil")
        self.employee("E2", segment="Bev")

        oil = self.board("JIVO_OIL")["headcount"]
        bev = self.board("JIVO_BEVERAGES")["headcount"]

        self.assertEqual(oil["total"], 2)
        self.assertEqual(bev["total"], 2)
        self.assertEqual(oil["scope"], "group")

    def test_people_on_leave_and_probation_are_still_on_the_rolls(self):
        """``ACTIVE`` alone would shrink the plant every time somebody took leave."""
        self.employee("E1", status=EmploymentStatus.ACTIVE)
        self.employee("E2", status=EmploymentStatus.ON_LEAVE)
        self.employee("E3", status=EmploymentStatus.PROBATION)
        self.employee("E4", status=EmploymentStatus.SUSPENDED)

        self.assertEqual(self.board()["headcount"]["total"], 4)

    def test_somebody_who_left_is_not_counted(self):
        self.employee("E1")
        self.employee("E2", status=EmploymentStatus.RESIGNED)

        self.assertEqual(self.board()["headcount"]["total"], 1)

    def test_a_blank_segment_is_labelled_not_dropped(self):
        """Dropping it is the tempting bug: the bars stop adding up to the total."""
        self.employee("E1", segment="Oil")
        self.employee("E2", segment="")

        segments = {row["label"]: row["count"] for row in self.board()["headcount"]["segments"]}

        self.assertEqual(segments, {"Oil": 1, "Unassigned": 1})
        self.assertEqual(sum(segments.values()), self.board()["headcount"]["total"])

    def test_people_with_no_department_are_counted_and_warned_about(self):
        self.employee("E1", department=self.canola)
        self.employee("E2", department=None)

        board = self.board()

        self.assertEqual(board["headcount"]["unassigned"], 1)
        self.assertTrue(
            any("no department" in warning for warning in board["meta"]["warnings"])
        )

    def test_no_joiner_or_attrition_figure_is_offered(self):
        """Every joining_date in the live directory is the bulk-import date and
        no record has ever reached an exit status, so any such tile would render
        a confident zero. Asserted so that adding one is a deliberate act."""
        self.employee("E1")

        headcount = self.board()["headcount"]

        for absent in ("joiners", "leavers", "attrition", "tenure"):
            self.assertNotIn(absent, headcount)


class BoardShapeTests(HrBoardTestCase):
    """The contract the front end reads."""

    def test_meta_carries_the_scope_and_the_cadence(self):
        meta = self.board()["meta"]

        self.assertEqual(meta["company"], "JIVO_OIL")
        self.assertEqual(meta["as_of"], TODAY.isoformat())
        self.assertEqual(meta["degraded"], [])
        self.assertEqual(meta["withheld"], [])
        self.assertGreater(meta["refresh_seconds"], 0)

    def test_both_sections_are_present_without_a_user(self):
        """``user=None`` withholds nothing -- the injectable-collaborator pattern."""
        board = self.board()

        self.assertIsNotNone(board["headcount"])
        self.assertIsNotNone(board["labour"])
