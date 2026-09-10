"""
Load the factory's own hierarchy workbook into the module.

    # look before you leap: prints the plan and every problem, writes nothing
    python manage.py import_factory_hierarchy --file "Factory Heirarchy.xlsx"

    # do it, replacing whatever is there
    python manage.py import_factory_hierarchy --file "Factory Heirarchy.xlsx" \
        --company JIVO_OIL --wipe --commit --backup /tmp/before.json

The source is one sheet, ``Master Data``, whose columns are a chart rather than
a table: ``Management | Department | HOD | Sub Department | L1 | Employee Name``
plus a designation and four cost-classification columns. Each row is one
*person at the bottom* with their whole chain repeated beside them, so the
managers exist only as names in columns and appear on dozens of rows.

Turning that into the module's tables means answering things a spreadsheet
never had to:

**Who counts as one person.** A name in a *manager* column is matched
case-insensitively across the whole sheet -- that is how the sheet refers back
to somebody, so ``Vicky Veer Ji`` the HOD and ``Vicky Veer ji`` the L1 are one
man. A name on an *Employee Name* row is one person **per row**, and is never
merged with another row or with a manager of the same name.

That asymmetry is deliberate, and it is the opposite of what it looks like.
Matching staff on name too would merge ``Mira Devi`` in Oil Production with
``Mira Devi`` in Housekeeping -- two different women, in different departments,
on different shifts -- and it would put ``Sumit`` the IT department head
underneath a warehouse supervisor, because a worker of the same name reports
there. Twelve people disappeared that way in the first run of this importer.
Two records for one person is a five-second fix for HR; a merged pair is a
person who has vanished from the payroll and a chart that lies. Every name
that collides across tiers is listed in the report.

**One manager each.** The sheet gives a person a superior on every row they
appear on, and those disagree — two HODs sit under two different Management
people. The importer takes the superior named most often across all of that
person's rows, and reports every disagreement it settled.

**Names that are not people.** ``Vacant`` is an unfilled post, ``193`` is a
stray number, and ``Bagha Purana`` is a town in the HOD column. None becomes an
employee; anybody who reported to one is attached to the next real manager up
and listed in the report.

**Rows with no name** are vacancies — a designation with nobody in it. No
employee is invented for them, but a supervisor they name is still created,
because they are a real person even when their post below them is empty.

Nothing is guessed silently. Every judgement lands in ``--report`` and in the
employee's own history, and the command refuses to write at all unless
``--commit`` is passed.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from company.models import Company
from employee_hierarchy import hierarchy, services
from employee_hierarchy.constants import AuditAction, EmploymentStatus, HistoryEvent
from employee_hierarchy.models import (
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    SalaryRevision,
)

SHEET = "Master Data"

#: The tiers, most senior first. The number is only used to rank appearances.
MANAGEMENT, HOD, L1, STAFF = 1, 2, 3, 4
TIER_NAME = {MANAGEMENT: "Management", HOD: "HOD", L1: "L1", STAFF: "Employee"}

#: Values that appear in a person column but are not a person.
#:
#: ``Vacant`` is an unfilled supervisor post. ``193`` is a stray number on an
#: otherwise empty row. ``Bagha Purana`` is a place — a town in Moga district —
#: entered in the HOD column, so whoever it stands for cannot be identified;
#: anybody under it is attached to the management above instead.
NOT_PEOPLE = {"vacant", "193", "bagha purana", ".", "-", "na", "n/a", "nil", "none"}

#: One cell naming two supervisors. The repo's own department ownership chart
#: (``org_chart.constants``) lists Prince and Gagan as the two L1s under
#: Prabhu Veerji's operations, which is what this cell means, so both people are
#: created and the staff on those rows go to the first named. Worth a human
#: confirming which of the two actually holds each person.
SPLIT_SUPERVISORS = {"gagan/prince": ["Gagan", "Prince"]}

#: Designation level guessed from the workbook's Category column. The file
#: carries no ladder, so these are a starting point for HR to correct, not a
#: claim -- which is why the report says so.
CATEGORY_LEVEL = {"executive": 5, "operator": 6, "worker": 7}
TIER_LEVEL = {MANAGEMENT: 1, HOD: 2, L1: 3}
DEFAULT_LEVEL = 5


def clean(value):
    """Collapse whitespace; ``None`` becomes empty."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def key_of(name):
    """The identity of a person: case- and spacing-insensitive."""
    return clean(name).casefold()


def is_person(name):
    return bool(clean(name)) and key_of(name) not in NOT_PEOPLE


def slug(text, used, limit=30):
    """A short unique CODE from a name: ``Production(Oil)`` -> ``PRODUCTION_OIL``."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", clean(text)).strip("_").upper()[:limit] or "X"
    candidate = base
    counter = 2
    while candidate in used:
        suffix = f"_{counter}"
        candidate = base[: limit - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


@dataclass
class Person:
    """One human, assembled from every row that mentions them."""

    name: str
    #: Most senior tier they appear in.
    tier: int = STAFF
    #: Every superior named above them, tier by tier, for the majority vote.
    superiors: Counter = field(default_factory=Counter)
    #: Departments and sub-departments they appear in, most common wins.
    departments: Counter = field(default_factory=Counter)
    sub_departments: Counter = field(default_factory=Counter)
    designations: Counter = field(default_factory=Counter)
    categories: Counter = field(default_factory=Counter)
    sap: Counter = field(default_factory=Counter)
    budget: Counter = field(default_factory=Counter)
    sub_budget: Counter = field(default_factory=Counter)
    #: Sheet rows they came from, so a correction can be traced back.
    rows: list = field(default_factory=list)
    #: ("mgr", name) or ("staff", name, row) -- see build_plan.
    identity: tuple = ()
    code: str = ""

    def note(self, tier, superior, row):
        self.tier = min(self.tier, tier)
        if superior and key_of(superior) != key_of(self.name):
            self.superiors[clean(superior)] += 1
        self.rows.append(row)


class Command(BaseCommand):
    help = "Import the factory hierarchy workbook into the employee module."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx workbook.")
        parser.add_argument("--company", default="JIVO_OIL", help="Company code to import into.")
        parser.add_argument(
            "--wipe",
            action="store_true",
            help="Delete every existing employee, department, designation, salary, "
            "history and audit row first.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without it the command reports what it would do and stops.",
        )
        parser.add_argument("--backup", help="Write the rows being deleted to this JSON file.")
        parser.add_argument("--report", help="Write the full decision report to this file.")

    # -- reading ---------------------------------------------------------

    def read_rows(self, path):
        import openpyxl

        workbook = openpyxl.load_workbook(path, data_only=True)
        if SHEET not in workbook.sheetnames:
            raise SystemExit(f"{path} has no {SHEET!r} sheet (found {workbook.sheetnames}).")
        sheet = workbook[SHEET]
        header = [clean(cell.value) for cell in sheet[1]]
        rows = []
        for index, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if all(value is None or not clean(value) for value in values):
                continue
            row = dict(zip(header, values))
            row["_excel_row"] = index
            rows.append(row)
        return rows

    # -- the plan --------------------------------------------------------

    def build_plan(self, rows):
        """Turn the sheet into people, departments and designations.

        Keys are ``("mgr", name)`` for somebody named as a supervisor and
        ``("staff", name, row)`` for somebody on an employee row -- see the
        module docstring for why staff are per-row.
        """
        people: dict[tuple, Person] = {}
        notes = defaultdict(list)

        def manager_person(name):
            identity = ("mgr", key_of(name))
            if identity not in people:
                people[identity] = Person(name=clean(name), identity=identity)
            return people[identity]

        def staff_person(name, row):
            identity = ("staff", key_of(name), row)
            people[identity] = Person(name=clean(name), identity=identity)
            return people[identity]

        skipped_rows = []
        for row in rows:
            excel_row = row["_excel_row"]
            management = clean(row.get("Management"))
            department = clean(row.get("Department"))
            hod = clean(row.get("HOD"))
            sub_department = clean(row.get("Sub Department"))
            supervisor = clean(row.get("L1"))
            staff = clean(row.get("Employee Name"))

            # A row with no department and no real person on it is not a row.
            if not department and not is_person(staff) and not is_person(hod):
                skipped_rows.append((excel_row, "nothing on it but a stray value"))
                notes["junk rows"].append(f"row {excel_row}: dropped ({clean(row.get('HOD')) or 'blank'})")
                continue

            # Who is the next real manager above each tier?
            above_hod = management if is_person(management) else ""
            above_l1 = hod if is_person(hod) else above_hod

            if is_person(management):
                entry = manager_person(management)
                entry.note(MANAGEMENT, "", excel_row)
                entry.departments[department] += 1 if department else 0

            if is_person(hod):
                entry = manager_person(hod)
                entry.note(HOD, above_hod, excel_row)
                if department:
                    entry.departments[department] += 1
            elif clean(hod):
                notes["not a person"].append(
                    f"row {excel_row}: HOD {clean(hod)!r} is not a name — "
                    f"anyone under it goes to {above_hod or 'the top'}"
                )

            supervisors = SPLIT_SUPERVISORS.get(key_of(supervisor))
            if supervisors:
                notes["one cell, two supervisors"].append(
                    f"row {excel_row}: L1 {supervisor!r} split into {supervisors}; "
                    f"staff attached to {supervisors[0]}"
                )
                for name in supervisors:
                    entry = manager_person(name)
                    entry.note(L1, above_l1, excel_row)
                    if department:
                        entry.departments[department] += 1
                    if sub_department:
                        entry.sub_departments[(department, sub_department)] += 1
                above_staff = supervisors[0]
            elif is_person(supervisor):
                entry = manager_person(supervisor)
                entry.note(L1, above_l1, excel_row)
                if department:
                    entry.departments[department] += 1
                if sub_department:
                    entry.sub_departments[(department, sub_department)] += 1
                above_staff = supervisor
            else:
                if clean(supervisor):
                    notes["not a person"].append(
                        f"row {excel_row}: L1 {clean(supervisor)!r} is not a name — "
                        f"staff attached to {above_l1 or 'the top'}"
                    )
                above_staff = above_l1

            if not is_person(staff):
                if clean(staff):
                    notes["not a person"].append(f"row {excel_row}: employee {clean(staff)!r} ignored")
                else:
                    notes["vacancies (no employee created)"].append(
                        f"row {excel_row}: {department} / {sub_department} — "
                        f"{clean(row.get('Designation')) or 'no designation'}"
                    )
                continue

            entry = staff_person(staff, excel_row)
            entry.note(STAFF, above_staff, excel_row)
            if department:
                entry.departments[department] += 1
            if sub_department:
                entry.sub_departments[(department, sub_department)] += 1
            for column, bucket in (
                ("Designation", entry.designations),
                ("Category", entry.categories),
                ("SAP", entry.sap),
                ("Budget", entry.budget),
                ("Sub Budget", entry.sub_budget),
            ):
                value = clean(row.get(column))
                if value and value != ".":
                    bucket[value] += 1

        return people, notes, skipped_rows

    def resolve_managers(self, people, notes):
        """Give every person exactly one manager, and report the disagreements."""
        managers = {}
        for identity, entry in people.items():
            if not entry.superiors:
                managers[identity] = None
                continue
            ranked = entry.superiors.most_common()
            chosen = ranked[0][0]
            # Only a manager can disagree with itself: a staff row names exactly
            # one supervisor, so a conflict here is always somebody who
            # supervises in two places.
            if len(ranked) > 1:
                notes["named under more than one manager"].append(
                    f"{entry.name} ({TIER_NAME[entry.tier]}): "
                    f"{', '.join(f'{name} x{count}' for name, count in ranked)} — used {chosen}"
                )
            managers[identity] = chosen
        return managers

    # -- writing ---------------------------------------------------------

    def wipe(self, backup_path):
        """Remove every row this module owns, after dumping it to JSON."""
        payload = {}
        for model in (
            SalaryRevision,
            EmployeeSalary,
            EmployeeHistory,
            EmployeeAuditLog,
            Employee,
            Department,
            Designation,
        ):
            rows = list(model.objects.values())
            payload[model.__name__] = json.loads(
                json.dumps(rows, default=str)
            )
        if backup_path:
            with open(backup_path, "w") as handle:
                json.dump(payload, handle, indent=1, default=str)
            self.stdout.write(f"  backup written to {backup_path}")

        counts = {}
        # Salary and revision first, then the trail, then the people they hang
        # off: the FKs are PROTECT in places and CASCADE in others, and doing it
        # in dependency order means never relying on which.
        for model in (
            SalaryRevision,
            EmployeeSalary,
            EmployeeHistory,
            EmployeeAuditLog,
        ):
            counts[model.__name__] = model.objects.all().delete()[0]
        # Employees point at each other and at departments; clear the pointers
        # before deleting so PROTECT never fires.
        Employee.objects.update(reporting_manager=None, department=None, designation=None)
        Department.objects.update(head=None, parent=None)
        counts["Employee"] = Employee.objects.all().delete()[0]
        counts["Department"] = Department.objects.all().delete()[0]
        counts["Designation"] = Designation.objects.all().delete()[0]
        return payload, counts

    def handle(self, *args, **options):
        path = options["file"]
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise SystemExit(f"No company with code {options['company']}.")

        rows = self.read_rows(path)
        people, notes, skipped = self.build_plan(rows)
        managers = self.resolve_managers(people, notes)

        # A name that is both a supervisor and a member of staff becomes two
        # records, because the sheet gives no way to tell whether it is one
        # person wearing two hats or two people sharing a name. Say so.
        manager_names = {
            identity[1] for identity in people if identity[0] == "mgr"
        }
        for identity, entry in sorted(people.items(), key=lambda item: str(item[0])):
            if identity[0] == "staff" and identity[1] in manager_names:
                notes["same name as a supervisor — imported as two records"].append(
                    f"{entry.name} (sheet row {identity[2]}) — check whether this is the "
                    f"same person as the supervisor of that name"
                )
        staff_names = Counter(
            identity[1] for identity in people if identity[0] == "staff"
        )
        for name, count in sorted(staff_names.items()):
            if count > 1:
                notes["same name on more than one staff row — imported separately"].append(
                    f"{name} appears on {count} employee rows"
                )

        # Department tree: the Department column is the parent, Sub Department
        # the child. Two sub-departments share a name across parents and seven
        # repeat their parent's name, and the module keeps names unique per
        # company, so both cases are resolved here rather than at the database.
        parents = {}
        children = {}
        child_names = Counter()
        for entry in people.values():
            for (parent, child), _ in entry.sub_departments.items():
                if parent and child:
                    child_names[child] += 0
        sub_pairs = set()
        for entry in people.values():
            for (parent, child) in entry.sub_departments:
                if parent and child:
                    sub_pairs.add((parent, child))
        shared = Counter(child for _, child in sub_pairs)
        for entry in people.values():
            for parent in entry.departments:
                if parent:
                    parents[parent] = None
        for parent, child in sorted(sub_pairs):
            parents.setdefault(parent, None)
            if key_of(child) == key_of(parent):
                # "Accounts / Accounts" is one department, not a child of itself.
                children[(parent, child)] = parent
                notes["sub-department same as its department"].append(
                    f"{parent}: staff placed on the department itself"
                )
                continue
            name = child if shared[child] == 1 else f"{child} ({parent})"
            if shared[child] > 1:
                notes["sub-department name used twice"].append(
                    f"{child!r} exists under {parent!r} — stored as {name!r}"
                )
            children[(parent, child)] = name

        report_lines = self.render_report(people, managers, notes, skipped, rows, parents, children)
        for line in report_lines:
            self.stdout.write(line)
        if options.get("report"):
            with open(options["report"], "w") as handle:
                handle.write("\n".join(report_lines) + "\n")
            self.stdout.write(f"\nreport written to {options['report']}")

        if not options["commit"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN — nothing was written. Re-run with --commit to apply."
                )
            )
            return

        with transaction.atomic():
            if options["wipe"]:
                self.stdout.write("\nwiping existing rows…")
                _, removed = self.wipe(options.get("backup"))
                for name, count in removed.items():
                    self.stdout.write(f"  deleted {count} {name}")
            created = self.write(company, people, managers, parents, children, path)

        self.stdout.write(self.style.SUCCESS("\nimported:"))
        for name, count in created.items():
            self.stdout.write(f"  {name}: {count}")

    def write(self, company, people, managers, parents, children, source_path):
        """Create the masters, then the people, then attach the tree."""
        used_codes = set()
        department_rows = {}
        for name in sorted(parents):
            department_rows[name] = Department.objects.create(
                company=company, code=slug(name, used_codes), name=name[:100]
            )
        for (parent, child), stored_name in sorted(children.items()):
            if stored_name == parent:
                continue
            if stored_name in department_rows:
                continue
            department_rows[stored_name] = Department.objects.create(
                company=company,
                code=slug(stored_name, used_codes),
                name=stored_name[:100],
                parent=department_rows.get(parent),
            )

        # Designations, with a level guessed from the Category column and from
        # the tier the holder sits in. The report says these are a guess.
        designation_tier = {}
        designation_category = {}
        for entry in people.values():
            for name in entry.designations:
                designation_tier[name] = min(designation_tier.get(name, STAFF), entry.tier)
                for category in entry.categories:
                    designation_category.setdefault(name, category)
        designation_codes = set()
        designation_rows = {}
        for name in sorted(designation_tier):
            tier = designation_tier[name]
            level = TIER_LEVEL.get(tier) or CATEGORY_LEVEL.get(
                key_of(designation_category.get(name, "")), DEFAULT_LEVEL
            )
            designation_rows[name] = Designation.objects.create(
                company=company,
                code=slug(name, designation_codes),
                name=name[:100],
                level=level,
                is_managerial=tier < STAFF,
                description="Level imported as a starting point — the source file carries no ladder.",
            )

        # People, in sheet order of first appearance so the codes run with the
        # workbook and a correction is easy to find.
        ordered = sorted(people.values(), key=lambda entry: (min(entry.rows), entry.tier))
        today = timezone.localdate()
        rows_by_identity = {}
        for index, entry in enumerate(ordered, start=1):
            entry.code = f"EMP{index:03d}"
            first, _, last = entry.name.partition(" ")
            department_name = self._department_for(entry, children)
            designation = None
            if entry.designations:
                designation = designation_rows.get(entry.designations.most_common(1)[0][0])
            employee = Employee(
                company=company,
                employee_code=entry.code,
                first_name=first[:100],
                last_name=last[:100],
                joining_date=today,
                employment_status=EmploymentStatus.ACTIVE,
                department=department_rows.get(department_name),
                designation=designation,
                job_title=(
                    entry.designations.most_common(1)[0][0]
                    if entry.designations
                    else (entry.categories.most_common(1)[0][0] if entry.categories else "")
                )[:100],
                is_manager=entry.tier < STAFF,
            )
            employee.save()
            hierarchy.place(employee, None)
            rows_by_identity[entry.identity] = employee

        # The tree, once everybody exists.
        attached = 0
        for identity, employee in rows_by_identity.items():
            manager_name = managers.get(identity)
            # A supervisor is always looked up in the manager namespace: the
            # sheet's manager columns are references to one person, while a
            # staff row of the same name is somebody else entirely.
            manager = (
                rows_by_identity.get(("mgr", key_of(manager_name))) if manager_name else None
            )
            if manager is None or manager.pk == employee.pk:
                continue
            hierarchy.move_to_manager(employee, manager)
            attached += 1

        # Department and sub-department heads: the HOD of a department, and the
        # supervisor most often named inside a sub-department.
        heads = 0
        for identity, employee in rows_by_identity.items():
            entry = people[identity]
            if entry.tier == HOD and entry.departments:
                name = entry.departments.most_common(1)[0][0]
                row = department_rows.get(name)
                if row is not None and row.head_id is None:
                    row.head = employee
                    row.save(update_fields=["head", "updated_at"])
                    heads += 1
            if entry.tier == L1 and entry.sub_departments:
                parent, child = entry.sub_departments.most_common(1)[0][0]
                row = department_rows.get(children.get((parent, child)))
                if row is not None and row.head_id is None:
                    row.head = employee
                    row.save(update_fields=["head", "updated_at"])
                    heads += 1

        # The trail: what each person is, where they came from, and what the
        # file did NOT say — so nobody mistakes an imported default for a fact.
        source = source_path.rsplit("/", 1)[-1]
        for identity, employee in rows_by_identity.items():
            entry = people[identity]
            tags = []
            for label, bucket in (
                ("SAP", entry.sap),
                ("Budget", entry.budget),
                ("Sub budget", entry.sub_budget),
                ("Category", entry.categories),
            ):
                if bucket:
                    tags.append(f"{label}: {bucket.most_common(1)[0][0]}")
            EmployeeHistory.objects.create(
                employee=employee,
                event=HistoryEvent.JOINED,
                occurred_on=today,
                to_value=TIER_NAME[entry.tier],
                notes=(
                    f"Imported from {source} (sheet rows "
                    f"{', '.join(str(row) for row in sorted(set(entry.rows))[:8])}). "
                    + (" · ".join(tags) + ". " if tags else "")
                    + "JOINING DATE NOT SUPPLIED by the source file — the date shown is the "
                    "import date and needs correcting."
                ),
            )
            EmployeeAuditLog.objects.create(
                employee=employee,
                action=AuditAction.EMPLOYEE_CREATED,
                new_value=f"{employee.employee_code} – {employee.full_name}",
                reason=f"Bulk import from {source}",
                notes="Created by manage.py import_factory_hierarchy.",
            )

        return {
            "departments": len(department_rows),
            "designations": len(designation_rows),
            "employees": len(rows_by_identity),
            "reporting lines attached": attached,
            "department heads set": heads,
        }

    @staticmethod
    def _department_for(entry, children):
        """Where a person sits: their sub-department if they have one, else their department."""
        if entry.sub_departments:
            parent, child = entry.sub_departments.most_common(1)[0][0]
            return children.get((parent, child), parent)
        if entry.departments:
            return entry.departments.most_common(1)[0][0]
        return ""

    # -- report ----------------------------------------------------------

    def render_report(self, people, managers, notes, skipped, rows, parents, children):
        lines = []
        add = lines.append
        add(self.style.MIGRATE_HEADING("Factory hierarchy import — plan"))
        add(f"  sheet rows read              : {len(rows)}")
        add(f"  people found                 : {len(people)}")
        tiers = Counter(entry.tier for entry in people.values())
        for tier in (MANAGEMENT, HOD, L1, STAFF):
            add(f"    {TIER_NAME[tier]:12}               : {tiers.get(tier, 0)}")
        add(f"  departments                  : {len(parents)}")
        add(f"  sub-departments              : {len([1 for name in children.values() if name not in parents])}")
        designations = {name for entry in people.values() for name in entry.designations}
        add(f"  designations                 : {len(designations)}")
        roots = [entry.name for identity, entry in people.items() if not managers.get(identity)]
        add(f"  top of the company           : {', '.join(sorted(roots)) or '(none)'}")
        add(f"  rows dropped                 : {len(skipped)}")

        add("")
        add(self.style.MIGRATE_HEADING("Judgements made — each of these is worth a human eye"))
        if not notes:
            add("  none")
        for heading in sorted(notes):
            add(f"\n  {heading} ({len(notes[heading])}):")
            for line in notes[heading][:40]:
                add(f"    - {line}")
            if len(notes[heading]) > 40:
                add(f"    … and {len(notes[heading]) - 40} more")

        add("")
        add(self.style.MIGRATE_HEADING("Not in the source file, so not imported"))
        add("  - joining dates    (import date used; flagged in every employee's history)")
        add("  - salaries         (no employee will have a salary record)")
        add("  - emails, phones, dates of birth, photos, app logins")
        add("  - designation levels are a guess from the Category column, not a ladder")
        return lines
