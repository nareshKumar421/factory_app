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


class ManagerWithOwnRowTests(ImportFixture):
    """An HOD who also has a staff row is one person, not two.

    Three of the real sheet's eleven HODs appear in the Name column as well.
    Importing both gives a synthetic ``MGR-nnn`` that the team reports to and
    the person's real row -- carrying their real JWPL code -- stranded at the
    top with nobody under them. Since that code is what the punching machines
    key on, the split files their attendance against the record that is not
    their job.

    The merge is guarded by the same rule as everything else here: a name that
    picks out more than one staff row is never matched.
    """

    def test_hod_with_own_row_becomes_one_record(self):
        rows = [
            ["Gagan Veerji", "JWPL0854", "Yasin Khan", "Civil", "HOD", "Civil", "Oil", ""],
            ["Gagan Veerji", "JWPL3067", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
            ["Gagan Veerji", "JWPL2764", "Rijvan", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        self.assertEqual(Employee.objects.filter(first_name="Yasin").count(), 1)
        yasin = Employee.objects.get(first_name="Yasin")
        self.assertEqual(yasin.employee_code, "JWPL0854", "keeps his real code, not MGR-nnn")
        self.assertTrue(yasin.is_manager)
        self.assertEqual(
            set(yasin.direct_reports.values_list("full_name", flat=True)),
            {"Ram Lal", "Rijvan"},
        )

    def test_merged_manager_is_placed_by_their_own_row(self):
        """His row names Gagan Veerji above him, so that is where he goes."""
        rows = [
            ["Gagan Veerji", "JWPL0854", "Yasin Khan", "Civil", "HOD", "Civil", "Oil", ""],
            ["Gagan Veerji", "JWPL3067", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), commit=True)

        yasin = Employee.objects.get(first_name="Yasin")
        self.assertEqual(yasin.reporting_manager.full_name, "Gagan Veerji")

    def test_merged_manager_with_no_one_on_their_row_falls_back_to_consensus(self):
        """Sanjay Sharma's own row names nobody; his team all sit under Gagan Veerji."""
        rows = [
            ["", "JWPL1440", "Sanjay Sharma", "Maintenance", "HOD", "Maintenance", "Oil", ""],
            ["Gagan Veerji", "JWPL2001", "Fitter One", "Maintenance", "Fitter", "Maintenance", "Oil", "Sanjay Sharma"],
        ]
        run(self.sheet(rows), commit=True)

        sanjay = Employee.objects.get(first_name="Sanjay")
        self.assertEqual(sanjay.employee_code, "JWPL1440")
        self.assertEqual(sanjay.reporting_manager.full_name, "Gagan Veerji")

    def test_ambiguous_manager_name_is_never_merged(self):
        """Two staff rows called Sandeep Singh: the HOD stays a separate record."""
        rows = [
            ["Gagan Veerji", "JWPL2609", "Sandeep Singh", "Gupta FG Ecom", "Fork Lift", "Ecom", "Oil", "Prabhu Veerji"],
            ["Gagan Veerji", "JWPL2610", "Sandeep Singh", "Production", "Worker", "Canola", "Oil", "Kulbeer Veerji"],
            ["Gagan Veerji", "JWPL2611", "Worker One", "Production", "Worker", "Canola", "Oil", "Sandeep Singh"],
        ]
        output = run(self.sheet(rows), commit=True)

        sandeeps = Employee.objects.filter(first_name="Sandeep").order_by("employee_code")
        self.assertEqual(sandeeps.count(), 3, "two staff rows plus the untouched HOD record")
        codes = sorted(s.employee_code for s in sandeeps)
        self.assertEqual(codes[:2], ["JWPL2609", "JWPL2610"])
        self.assertTrue(codes[2].startswith("MGR-"), codes)
        # The fork-lift operator keeps his own manager -- he was not promoted.
        forklift = Employee.objects.get(employee_code="JWPL2609")
        self.assertEqual(forklift.reporting_manager.full_name, "Prabhu Veerji")
        self.assertFalse(forklift.is_manager)
        self.assertIn("shares a name with more than one staff row", output)

    def test_merge_is_reported(self):
        rows = [
            ["Gagan Veerji", "JWPL0854", "Yasin Khan", "Civil", "HOD", "Civil", "Oil", ""],
            ["Gagan Veerji", "JWPL3067", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        output = run(self.sheet(rows), commit=True)

        self.assertIn("merged with their own staff row", output)
        self.assertIn("JWPL0854", output)

    def test_manager_with_no_row_still_gets_a_synthetic_record(self):
        """The other eight HODs have no row, and must still exist."""
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Common", "Shunty Veerji"],
        ]
        run(self.sheet(rows), commit=True)

        shunty = Employee.objects.get(first_name="Shunty")
        self.assertTrue(shunty.employee_code.startswith("MGR-"))
        self.assertTrue(shunty.is_manager)


class CodeOnlyUpdateTests(ImportFixture):
    """``--update-codes``: write the two columns the punches need, nothing else.

    The rebuild is the right tool when the *structure* is wrong. It is the
    wrong tool when only the codes are missing, because it deletes salaries,
    revisions, history and user links to get there. This mode exists so the
    directory can gain its codes without betting any of that on the sheet,
    which is why these tests are mostly about what does *not* move.
    """

    def directory(self, people):
        """Seed a live-looking directory: ``[(code, first, last), ...]``."""
        made = {}
        for code, first, last in people:
            made[code] = Employee.objects.create(
                company=self.company, employee_code=code, first_name=first, last_name=last
            )
        return made

    def test_code_and_segment_are_written_onto_the_existing_row(self):
        self.directory([("EMP001", "Vishal", "Tyagi")])
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        employee = Employee.objects.get(first_name="Vishal")
        self.assertEqual(employee.employee_code, "JWPL0593")
        self.assertEqual(employee.sap_segment, "Oil")

    def test_the_row_survives_intact(self):
        """Same primary key, same manager, same everything else."""
        people = self.directory([("EMP001", "Vishal", "Tyagi"), ("EMP002", "Ram", "Lal")])
        boss, worker = people["EMP001"], people["EMP002"]
        worker.reporting_manager = boss
        worker.save()
        hierarchy.rebuild_paths()
        before = Employee.objects.get(pk=worker.pk)

        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
            ["Gagan Veerji", "JWPL3067", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Vishal Tyagi"],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        after = Employee.objects.get(pk=worker.pk)
        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.reporting_manager_id, boss.pk)
        self.assertEqual(after.hierarchy_path, before.hierarchy_path)
        self.assertEqual(after.employee_code, "JWPL3067")

    def test_nothing_is_created_or_deleted(self):
        """The sheet is a source of codes here, not a source of people."""
        self.directory([("EMP001", "Vishal", "Tyagi"), ("EMP900", "Ghost", "Worker")])
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", "Shunty Veerji"],
            ["Gagan Veerji", "JWPL3067", "Nobody Here", "Admin", "Gardner", "Housekeeping", "Common", "Yasin Khan"],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(Employee.objects.count(), 2)
        # The stranger keeps the code they had; the sheet never mentions them.
        self.assertEqual(
            Employee.objects.get(first_name="Ghost").employee_code, "EMP900"
        )
        # No synthetic manager records, no departments, no designations.
        self.assertFalse(Employee.objects.filter(employee_code__startswith="MGR-").exists())
        self.assertEqual(Department.objects.count(), 0)
        self.assertEqual(Designation.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        self.directory([("EMP001", "Vishal", "Tyagi")])
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        output = run(self.sheet(rows), update_codes=True)

        self.assertEqual(Employee.objects.get(first_name="Vishal").employee_code, "EMP001")
        self.assertIn("Dry run", output)
        self.assertIn("EMP001 -> JWPL0593", output)

    def test_a_name_on_two_directory_rows_is_skipped(self):
        """Two Harpreet Singhs: guessing puts a code on the wrong man."""
        self.directory([("EMP001", "Harpreet", "Singh"), ("EMP002", "Harpreet", "Singh")])
        rows = [
            ["Gagan Veerji", "JWPL0785", "Harpreet Singh", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        output = run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(Employee.objects.filter(employee_code__startswith="EMP").count(), 2)
        self.assertIn("more than one directory row", output)

    def test_resolve_settles_an_ambiguous_name(self):
        people = self.directory([("EMP001", "Sandeep", "Singh"), ("EMP002", "Sandeep", "Singh")])
        target = people["EMP002"]
        rows = [
            ["Gagan Veerji", "JWPL2884", "Sandeep Singh", "Audit", "HOD", "Audit", "Oil", ""],
        ]
        output = run(
            self.sheet(rows), update_codes=True, commit=True, resolve=[f"2={target.pk}"]
        )

        target.refresh_from_db()
        self.assertEqual(target.employee_code, "JWPL2884")
        self.assertEqual(people["EMP001"].employee_code, "EMP001")
        self.assertIn("settled by hand", output)

    def test_a_person_with_no_code_keeps_the_one_they_have(self):
        """NOCODE-nnnn is the sheet admitting it has none, not an identity.

        Writing it would destroy a real code and still match no punch.
        """
        self.directory([("EMP001", "Shubham", "")])
        rows = [
            ["Gagan Veerji", "New Sep", "Shubham", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        output = run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(Employee.objects.get(first_name="Shubham").employee_code, "EMP001")
        self.assertIn("sheet has no code for them", output)

    def test_a_code_held_by_somebody_else_is_skipped(self):
        self.directory([("EMP001", "Vishal", "Tyagi"), ("JWPL0593", "Ram", "Lal")])
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        output = run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(Employee.objects.get(first_name="Vishal").employee_code, "EMP001")
        self.assertEqual(Employee.objects.get(first_name="Ram").employee_code, "JWPL0593")
        self.assertIn("already belongs to somebody else", output)

    def test_two_people_can_swap_codes(self):
        """``(company, employee_code)`` is unique, so a cycle needs two passes."""
        self.directory([("JWPL0002", "Vishal", "Tyagi"), ("JWPL0001", "Ram", "Lal")])
        rows = [
            ["Gagan Veerji", "JWPL0001", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
            ["Gagan Veerji", "JWPL0002", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", ""],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(Employee.objects.get(first_name="Vishal").employee_code, "JWPL0001")
        self.assertEqual(Employee.objects.get(first_name="Ram").employee_code, "JWPL0002")
        self.assertFalse(Employee.objects.filter(employee_code__startswith="TMP-").exists())

    def test_salaries_and_history_are_untouched(self):
        """The whole reason this mode exists."""
        from decimal import Decimal

        from .constants import HistoryEvent
        from .models import EmployeeSalary

        people = self.directory([("EMP001", "Vishal", "Tyagi")])
        employee = people["EMP001"]
        EmployeeSalary.objects.create(
            employee=employee, basic_salary=Decimal("100"), effective_from="2026-01-01"
        )
        EmployeeHistory.objects.create(
            employee=employee, event=HistoryEvent.JOINED, occurred_on="2026-01-01"
        )

        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        self.assertEqual(EmployeeSalary.objects.filter(employee=employee).count(), 1)
        self.assertEqual(EmployeeHistory.objects.filter(employee=employee).count(), 1)

    def test_the_change_is_audited(self):
        """A code move is an administrative act somebody may have to explain."""
        from .models import EmployeeAuditLog

        self.directory([("EMP001", "Vishal", "Tyagi")])
        rows = [
            ["Gagan Veerji", "JWPL0593", "Vishal Tyagi", "Accounts", "Accountant", "Accounts", "Oil", ""],
        ]
        run(self.sheet(rows), update_codes=True, commit=True)

        entry = EmployeeAuditLog.objects.get(field="employee_code")
        self.assertEqual(entry.previous_value, "EMP001")
        self.assertEqual(entry.new_value, "JWPL0593")


class HodAmbiguityTests(ImportFixture):
    """Sandeep Singh: the one ambiguity HR had to settle.

    Row 24 carries Designation ``HOD``; row 64 is a fork-lift operator in Gupta
    FG Ecom. The rebuild merges the HOD into row 24 on that basis. Pinned here
    because a re-cut sheet that moves those rows must fail loudly.
    """

    def test_hr_named_row_is_merged(self):
        rows = [
            # Two namesakes; the module-level override says the manager is the first.
            ["Gagan Veerji", "JWPL2884", "Sandeep Singh", "Audit", "HOD", "Audit", "Oil", ""],
            ["Gagan Veerji", "JWPL2609", "Sandeep Singh", "Gupta FG Ecom", "Fork Lift", "Ecom", "Mart", "Prabhu Veerji"],
            ["Gagan Veerji", "JWPL3067", "Ram Lal", "Admin", "Gardner", "Housekeeping", "Common", "Sandeep Singh"],
        ]
        from .management.commands import import_hierarchy_jwpl as command

        # The override is keyed to the real workbook's row numbers; this sheet
        # puts the HOD on row 2, so point it there for the test.
        original = dict(command.HOD_STAFF_ROW)
        command.HOD_STAFF_ROW.clear()
        command.HOD_STAFF_ROW["sandeep singh"] = 2
        try:
            output = run(self.sheet(rows), commit=True)
        finally:
            command.HOD_STAFF_ROW.clear()
            command.HOD_STAFF_ROW.update(original)

        self.assertIn("HR named which", output)
        # One record for the HOD, carrying his real code, with the team on him.
        hod = Employee.objects.get(employee_code="JWPL2884")
        self.assertEqual(hod.direct_reports.count(), 1)
        # Exactly one Sandeep Singh record per row -- no synthetic third one.
        self.assertEqual(Employee.objects.filter(last_name="Singh").count(), 2)
        # The fork-lift operator stays a separate person.
        self.assertTrue(Employee.objects.filter(employee_code="JWPL2609").exists())
