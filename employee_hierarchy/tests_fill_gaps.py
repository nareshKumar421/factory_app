"""
Tests for the chart-workbook gap fill.

This command writes onto people who already exist, from a sheet that is *older*
than the one that gave them their employee codes. So the tests are mostly about
restraint: what it must not touch, and what it must refuse to guess.

The matching rule carries the risk. The two workbooks disagree on spellings, and
some of those pairs are one person while others are two. Now that attendance is
keyed on these same rows, attaching the wrong man to a supervisor puts a
stranger's punches under his name -- which is why nothing is matched on
similarity, only reported.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from company.models import Company

from .models import Department, Employee

HEADER = [
    "S.No.", "Management", "Department", "HOD", "Sub Department", "L1",
    "Employee Name", "Designation", "Category", "SAP", "Sub Budget", "Budget",
]


def workbook(rows, path):
    """A sheet shaped like the real ``Master Data``; ``rows`` are data rows."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Master Data"
    sheet.append(HEADER)
    for index, row in enumerate(rows, start=1):
        sheet.append([index, *row])
    book.save(path)
    return str(path)


def run(path, **options):
    out = StringIO()
    call_command("fill_hierarchy_gaps", file=path, stdout=out, stderr=out, **options)
    return out.getvalue()


class Fixture(TestCase):
    def setUp(self):
        self.company, _ = Company.objects.get_or_create(
            code="JIVO_OIL", defaults={"name": "Jivo Oil"}
        )
        self.hod = self.person("JWPL0001", "Sanjay", "Sharma")
        self.l1 = self.person("JWPL0002", "Gurpal", "Singh")
        self.worker = self.person("JWPL0003", "Rohit", "Kumar")

    def person(self, code, first, last, **extra):
        return Employee.objects.create(
            company=self.company, employee_code=code,
            first_name=first, last_name=last, **extra
        )

    def sheet(self, rows):
        import pathlib
        import tempfile

        return workbook(rows, pathlib.Path(tempfile.mkdtemp()) / "chart.xlsx")

    def chain_row(self, employee="Rohit Kumar", l1="Gurpal Singh", **over):
        """One sheet row carrying a full chain, with the finance columns."""
        return [
            over.get("management", "Arvinder Veer Ji"),
            over.get("department", "Maintenance"),
            over.get("hod", "Sanjay Sharma"),
            over.get("sub_department", "Maintenance Frontend"),
            l1,
            employee,
            over.get("designation", "Utility Engineer"),
            over.get("category", "Worker"),
            over.get("sap", "BEV"),
            over.get("sub_budget", "Factory-WG"),
            over.get("budget", "Maintenance"),
        ]


class SupervisorLayerTests(Fixture):
    """The L1 column is the whole reason this command exists."""

    def test_the_worker_is_placed_under_their_l1(self):
        run(self.sheet([self.chain_row()]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.reporting_manager_id, self.l1.pk)

    def test_the_l1_is_placed_under_their_hod(self):
        """staff -> L1 -> HOD: the tier the JWPL sheet could not express."""
        run(self.sheet([self.chain_row()]), commit=True)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.reporting_manager_id, self.hod.pk)

    def test_the_chain_is_three_deep_afterwards(self):
        run(self.sheet([self.chain_row()]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.hierarchy_level, 3)

    def test_a_supervisor_who_is_not_in_the_directory_leaves_the_team_alone(self):
        """Inventing them would hang a team off somebody with no employee code
        and therefore no attendance."""
        output = run(self.sheet([self.chain_row(l1="Nobody Here")]), commit=True)
        self.worker.refresh_from_db()
        self.assertIsNone(self.worker.reporting_manager_id)
        self.assertIn("supervisor not in the directory", output)

    def test_vacant_is_not_a_supervisor(self):
        run(self.sheet([self.chain_row(l1="Vacant")]), commit=True)
        self.worker.refresh_from_db()
        # Falls through to the HOD rather than to a post called "Vacant".
        self.assertEqual(self.worker.reporting_manager_id, self.hod.pk)

    def test_no_supervisors_leaves_the_tree_untouched(self):
        run(self.sheet([self.chain_row()]), commit=True, no_supervisors=True)
        self.worker.refresh_from_db()
        self.assertIsNone(self.worker.reporting_manager_id)


class FinanceColumnTests(Fixture):
    def test_the_finance_columns_are_written(self):
        run(self.sheet([self.chain_row()]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.category, "Worker")
        self.assertEqual(self.worker.budget, "Maintenance")
        self.assertEqual(self.worker.sub_budget, "Factory-WG")

    def test_a_blank_cell_never_blanks_a_value_already_there(self):
        """This sheet is the older one. Silence in it is not a correction."""
        self.worker.budget = "Canola-Labour"
        self.worker.save(update_fields=["budget"])
        run(self.sheet([self.chain_row(budget="")]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.budget, "Canola-Labour")

    def test_the_employee_code_is_never_touched(self):
        """The whole point: this sheet has no code column, so it must not be
        allowed to have an opinion about codes."""
        run(self.sheet([self.chain_row()]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.employee_code, "JWPL0003")


class MatchingTests(Fixture):
    def test_a_near_miss_is_reported_and_never_matched(self):
        output = run(self.sheet([self.chain_row(employee="Rohit Kumaar")]), commit=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.category, "")
        self.assertIn("not in the directory", output)
        self.assertIn("Rohit Kumar", output)  # offered as the candidate

    def test_an_alias_applies_a_match_you_confirmed(self):
        run(
            self.sheet([self.chain_row(employee="Rohit Kumaar")]),
            commit=True,
            alias=["Rohit Kumaar=Rohit Kumar"],
        )
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.category, "Worker")
        self.assertEqual(self.worker.reporting_manager_id, self.l1.pk)

    def test_an_alias_naming_somebody_absent_is_refused(self):
        with self.assertRaises(SystemExit):
            run(self.sheet([self.chain_row()]), alias=["Someone=Nobody At All"])

    def test_a_name_on_two_directory_rows_is_skipped(self):
        self.person("JWPL0009", "Rohit", "Kumar")
        output = run(self.sheet([self.chain_row()]), commit=True)
        self.assertIn("more than one directory row", output)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.category, "")

    def test_a_surname_difference_is_suggested_but_not_applied(self):
        """'Gurparvez' and 'Gurparvez Singh' are one man; the command still
        makes a human say so."""
        self.person("NOCODE-0001", "Gurparvez", "")
        output = run(self.sheet([self.chain_row(employee="Gurparvez Singh")]), commit=True)
        self.assertIn("one of them with a surname", output)
        self.assertEqual(Employee.objects.get(employee_code="NOCODE-0001").category, "")

    def test_an_ambiguous_shortening_offers_nothing(self):
        """Three Sandeeps means no obvious answer, so it must not name one."""
        self.person("JWPL0010", "Sandeep", "Kujur")
        self.person("JWPL0011", "Sandeep", "Singh")
        output = run(self.sheet([self.chain_row(employee="Sandeep")]), commit=True)
        line = next(l for l in output.splitlines() if l.strip().startswith("- Sandeep"))
        self.assertIn("no candidate", line)


class DryRunTests(Fixture):
    def test_a_dry_run_writes_nothing(self):
        output = run(self.sheet([self.chain_row()]))
        self.worker.refresh_from_db()
        self.assertIsNone(self.worker.reporting_manager_id)
        self.assertEqual(self.worker.category, "")
        self.assertIn("Dry run", output)


class CreateMissingTests(Fixture):
    def test_nobody_is_created_by_default(self):
        run(self.sheet([self.chain_row(employee="Brand New")]), commit=True)
        self.assertFalse(Employee.objects.filter(first_name="Brand").exists())

    def test_create_missing_adds_them_without_a_code(self):
        run(self.sheet([self.chain_row(employee="Brand New")]), commit=True, create_missing=True)
        created = Employee.objects.get(first_name="Brand")
        self.assertTrue(created.employee_code.startswith("NOCODE-C"))
        self.assertEqual(created.budget, "Maintenance")

    def test_a_created_person_is_then_placed_under_their_supervisor(self):
        """The second planning pass: they cannot be attached until they exist."""
        run(self.sheet([self.chain_row(employee="Brand New")]), commit=True, create_missing=True)
        created = Employee.objects.get(first_name="Brand")
        self.assertEqual(created.reporting_manager_id, self.l1.pk)

    def test_a_created_supervisor_collects_their_team(self):
        rows = [
            self.chain_row(employee="Rohit Kumar", l1="New Supervisor"),
            self.chain_row(employee="New Supervisor", l1="Gurpal Singh"),
        ]
        run(self.sheet(rows), commit=True, create_missing=True)
        supervisor = Employee.objects.get(first_name="New", last_name="Supervisor")
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.reporting_manager_id, supervisor.pk)

    def test_their_department_is_reused_when_it_exists(self):
        Department.objects.create(company=self.company, name="Maintenance", code="MAINT")
        run(self.sheet([self.chain_row(employee="Brand New")]), commit=True, create_missing=True)
        created = Employee.objects.get(first_name="Brand")
        self.assertIsNotNone(created.department)
        self.assertEqual(created.department.name, "Maintenance")

    def test_running_create_missing_twice_does_not_duplicate(self):
        """A plain re-run is safe on its own: the second pass finds the person
        the first one created, through the ordinary directory lookup."""
        sheet = self.sheet([self.chain_row(employee="Brand New")])
        run(sheet, commit=True, create_missing=True)
        run(sheet, commit=True, create_missing=True)

        self.assertEqual(Employee.objects.filter(first_name="Brand").count(), 1)

    def test_a_record_an_alias_now_reassigns_is_reported_as_a_leftover(self):
        """The footgun that actually bit.

        Run one creates `Gurparvez Singh` because nothing matched. Later somebody
        confirms that he is the `Gurparvez` already on the roll. The alias makes
        run one's record a leftover -- and it is not harmless, because a team may
        have been placed under it, hanging off somebody with no employee code and
        therefore no attendance. It is reported rather than deleted: by then it
        may carry history of its own.
        """
        self.person("NOCODE-0001", "Gurparvez", "")
        sheet = self.sheet([self.chain_row(employee="Gurparvez Singh")])
        run(sheet, commit=True, create_missing=True)      # creates the duplicate

        output = run(sheet, commit=True, alias=["Gurparvez Singh=Gurparvez"])

        self.assertIn("LEFTOVER from an earlier run", output)
        self.assertIn("delete it", output)
        # Still there -- reported, never silently removed.
        self.assertTrue(Employee.objects.filter(full_name="Gurparvez Singh").exists())
