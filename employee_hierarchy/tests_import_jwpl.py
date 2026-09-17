"""
Tests for the JWPL hierarchy importer.

The importer's job is not "read a spreadsheet" -- that part is trivial. Its job
is to make a series of judgements about ambiguous data and be *honest* about
every one of them, and it is those judgements that are worth pinning down,
because each was a real defect in the previous import.

**The employee code is the join key to the punching machines.** A wrong code is
not an error anyone sees; it is somebody else's attendance shown against your
name. So: codes are taken verbatim, ``New Sep`` is never mistaken for one, a
person with no code still gets imported (they exist) under a code that visibly
cannot match anything, and two people can never end up sharing one.

**The supervisor layer has to survive.** The new sheet is three tiers; the
directory it replaces is four. The tests hold the three guards that decide when
an old supervisor link may be carried over and when the sheet wins.

**Nothing writes without --commit.** A dry run that touched the database would
be the single worst bug this command could have, given it wipes the directory.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from company.models import Company

from . import hierarchy
from .models import Department, Designation, Employee, EmployeeHistory

HEADER = [
    "S.NO", "Management", "JWPL", "Name", "Department",
    "Designation", "Sub Department", "SAP", "HOD",
]


def workbook(rows, path):
    """Write a sheet shaped like the real one. ``rows`` are data rows only."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(HEADER)
    for index, row in enumerate(rows, start=1):
        sheet.append([index, *row])
    book.save(path)
    return str(path)


def run(path, **options):
    out = StringIO()
    call_command("import_hierarchy_jwpl", file=path, stdout=out, stderr=out, **options)
    return out.getvalue()


class ImportFixture(TestCase):
    def setUp(self):
        self.company, _ = Company.objects.get_or_create(
            code="JIVO_OIL", defaults={"name": "Jivo Oil"}
        )

    def sheet(self, rows):
        return workbook(rows, self._tmp("book.xlsx"))

    def _tmp(self, name):
        import tempfile
        import pathlib

        directory = pathlib.Path(tempfile.mkdtemp())
        return directory / name


class DryRunTests(ImportFixture):
    """A dry run reports and writes nothing. It wipes the directory otherwise."""

    def test_dry_run_writes_nothing(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
        ]
        output = run(self.sheet(rows))

        self.assertEqual(Employee.objects.count(), 0)
        self.assertEqual(Department.objects.count(), 0)
        self.assertIn("Dry run", output)

    def test_commit_writes(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        self.assertEqual(Employee.objects.filter(employee_code="JWPL0001").count(), 1)
        self.assertTrue(Department.objects.filter(name="Accounts").exists())


class EmployeeCodeTests(ImportFixture):
    """The code is the punch-machine join key. Every rule about it matters."""

    def test_code_is_taken_verbatim(self):
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
            # Contract staff punch too -- a TP code is a code like any other.
            ["Gagan Veerji", "TP080", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        self.assertTrue(Employee.objects.filter(employee_code="JWPL0593").exists())
        self.assertTrue(Employee.objects.filter(employee_code="TP080").exists())

    def test_new_sep_is_not_a_code(self):
        """``New Sep`` is HR's note that the person is not enrolled yet.

        It sits in the JWPL column on more than one row, so treating it as a
        code would merge unrelated people onto one identity -- and then show one
        man's punches against the other's name.
        """
        rows = [
            ["Gagan Veerji", "New Sep", "Shubham", "Production", "Operator", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "New Sep", "Deepak Kumar", "Production", "Operator", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        codes = set(Employee.objects.values_list("employee_code", flat=True))
        # two staff, plus the HOD and the Management person named above them
        self.assertEqual(Employee.objects.count(), 4)
        self.assertNotIn("NEW SEP", codes)
        # Both exist, under codes that visibly cannot match a punch record.
        self.assertIn("NOCODE-0001", codes)
        self.assertIn("NOCODE-0002", codes)

    def test_person_with_no_code_is_still_imported(self):
        rows = [
            ["Gagan Veerji", None, "Ajab Singh Rana", "Admin", "Driver", "Transport", "Common", "Yasin Khan"],
        ]
        output = run(self.sheet(rows), commit=True)

        person = Employee.objects.get(full_name="Ajab Singh Rana")
        self.assertTrue(person.employee_code.startswith("NOCODE"))
        self.assertIn("cannot be matched to punch data", output)

    def test_duplicate_code_never_collapses_two_people(self):
        """Two rows quoting one code are two people, and both must survive."""
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
            ["Gagan Veerji", "JWPL0001", "Someone Else", "Accounts", "Clerk", "Accounts", "Oil", "Shunty Veerji"],
        ]
        output = run(self.sheet(rows), commit=True)

        self.assertEqual(Employee.objects.filter(full_name="Vishal Tyagi").count(), 1)
        self.assertEqual(Employee.objects.filter(full_name="Someone Else").count(), 1)
        self.assertIn("duplicate employee code", output)

    def test_sap_segment_is_normalised(self):
        """The sheet spells the same plant ``BEV`` and ``Bev``."""
        rows = [
            ["Gagan Veerji", "JWPL0001", "A One", "Production", "Operator", "Water Production", "BEV", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "B Two", "Production", "Operator", "Water Production", "Bev", "Kulbeer Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        segments = set(
            Employee.objects.filter(employee_code__startswith="JWPL").values_list(
                "sap_segment", flat=True
            )
        )
        self.assertEqual(segments, {"Bev"})


class TreeShapeTests(ImportFixture):
    """Management -> HOD -> staff, including managers who have no row."""

    def test_managers_named_only_in_columns_are_created(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        top = Employee.objects.get(full_name="Gagan Veerji")
        hod = Employee.objects.get(full_name="Shunty Veerji")
        staff = Employee.objects.get(employee_code="JWPL0001")

        self.assertIsNone(top.reporting_manager_id)
        self.assertEqual(hod.reporting_manager_id, top.pk)
        self.assertEqual(staff.reporting_manager_id, hod.pk)
        self.assertEqual(staff.hierarchy_level, 3)

    def test_sub_department_hangs_under_its_department(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        child = Department.objects.get(name="Housekeeping")
        self.assertEqual(child.parent.name, "Admin")
        self.assertEqual(Employee.objects.get(employee_code="JWPL0001").department_id, child.pk)

    def test_designations_are_created_once_per_name(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "A One", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
            ["Gagan Veerji", "JWPL0002", "B Two", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        self.assertEqual(Designation.objects.filter(name="Gardner").count(), 1)

    def test_two_workers_sharing_a_name_stay_two_people(self):
        """The lesson the previous import paid for: never merge staff on name."""
        rows = [
            ["Gagan Veerji", "JWPL0001", "Mira Devi", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "Mira Devi", "Admin", "Sweaper", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        self.assertEqual(Employee.objects.filter(full_name="Mira Devi").count(), 2)


class SupervisorPreservationTests(ImportFixture):
    """The layer the new sheet does not have, and the three guards on it."""

    def _existing_tree(self, *, worker="Worker One", supervisor="Super Visor", hod="Kulbeer Veerji"):
        """Build a four-tier directory: top -> hod -> supervisor -> worker."""
        top = Employee.objects.create(company=self.company, employee_code="OLD-1", first_name="Gagan", last_name="Veerji")
        hierarchy.place(top, None)
        head = Employee.objects.create(company=self.company, employee_code="OLD-2", first_name=hod.split()[0], last_name=" ".join(hod.split()[1:]))
        hierarchy.place(head, top)
        boss = Employee.objects.create(company=self.company, employee_code="OLD-3", first_name=supervisor.split()[0], last_name=" ".join(supervisor.split()[1:]))
        hierarchy.place(boss, head)
        hand = Employee.objects.create(company=self.company, employee_code="OLD-4", first_name=worker.split()[0], last_name=" ".join(worker.split()[1:]))
        hierarchy.place(hand, boss)
        return top, head, boss, hand

    def test_supervisor_is_spliced_back_in(self):
        self._existing_tree()
        rows = [
            ["Gagan Veerji", "JWPL0001", "Super Visor", "Production", "Supervisor", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "Worker One", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        worker = Employee.objects.get(employee_code="JWPL0002")
        supervisor = Employee.objects.get(employee_code="JWPL0001")
        hod = Employee.objects.get(full_name="Kulbeer Veerji")

        # The sheet alone would have put the worker straight under the HOD.
        self.assertEqual(worker.reporting_manager_id, supervisor.pk)
        self.assertEqual(supervisor.reporting_manager_id, hod.pk)
        self.assertEqual(worker.hierarchy_level, 4)

    def test_honorific_spelling_drift_still_matches(self):
        """``Vicky Veer Ji`` in the old directory is ``Vicky Veerji`` on the sheet."""
        self._existing_tree(supervisor="Vicky Veer Ji")
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vicky Veerji", "Production", "Supervisor", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "Worker One", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        worker = Employee.objects.get(employee_code="JWPL0002")
        self.assertEqual(worker.reporting_manager.full_name, "Vicky Veerji")

    def test_sheet_wins_when_the_supervisor_moved_to_another_hod(self):
        self._existing_tree()
        rows = [
            # The supervisor is now under a different HOD than the worker.
            ["Gagan Veerji", "JWPL0001", "Super Visor", "Admin", "Supervisor", "Housekeeping", "Common", "Yasin Khan"],
            ["Gagan Veerji", "JWPL0002", "Worker One", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        output = run(self.sheet(rows), commit=True)

        worker = Employee.objects.get(employee_code="JWPL0002")
        self.assertEqual(worker.reporting_manager.full_name, "Kulbeer Veerji")
        self.assertIn("different HOD", output)

    def test_ambiguous_name_is_never_matched(self):
        """Two old rows called the same thing cannot identify anybody."""
        top, head, boss, _hand = self._existing_tree(worker="Deepak")
        twin = Employee.objects.create(
            company=self.company, employee_code="OLD-5", first_name="Deepak"
        )
        hierarchy.place(twin, head)

        rows = [
            ["Gagan Veerji", "JWPL0001", "Super Visor", "Production", "Supervisor", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "Deepak", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        output = run(self.sheet(rows), commit=True)

        worker = Employee.objects.get(employee_code="JWPL0002")
        self.assertEqual(worker.reporting_manager.full_name, "Kulbeer Veerji")
        self.assertIn("more than one row in the old directory", output)

    def test_flag_turns_preservation_off(self):
        self._existing_tree()
        rows = [
            ["Gagan Veerji", "JWPL0001", "Super Visor", "Production", "Supervisor", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL0002", "Worker One", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
        ]
        run(self.sheet(rows), commit=True, no_preserve_supervisors=True)

        worker = Employee.objects.get(employee_code="JWPL0002")
        self.assertEqual(worker.reporting_manager.full_name, "Kulbeer Veerji")

    def test_import_leaves_a_trail(self):
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        person = Employee.objects.get(employee_code="JWPL0001")
        entry = EmployeeHistory.objects.get(employee=person)
        self.assertIn("sheet row", entry.notes)
