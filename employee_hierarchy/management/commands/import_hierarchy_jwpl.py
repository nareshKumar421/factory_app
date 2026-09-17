"""
Load the JWPL-coded hierarchy workbook, keeping the supervisor layer it lost.

    # look before you leap: prints the plan and every problem, writes nothing
    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx"

    # do it
    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx" \
        --company JIVO_OIL --commit --backup /tmp/before.json --report /tmp/report.txt

This is a **second** importer, not a replacement for
``import_factory_hierarchy``. That one reads a chart-shaped sheet
(``Management | Department | HOD | Sub Department | L1 | Employee Name``) where
managers exist only as names in columns. This one reads a flat person-per-row
sheet::

    S.NO | Management | JWPL | Name | Department | Designation | Sub Department | SAP | HOD

Two files rather than one parser with a mode flag, because the whole judgement
apparatus of the other command -- majority-vote superiors, split supervisor
cells, per-row staff identity -- exists to compensate for a layout this sheet
does not have.

**Why this import happened at all.** The live directory had no employee codes.
The punching machines key every record on the JWPL code, so until the directory
carried those codes there was no way to say which row of 432,000 punches
belonged to which employee. That is what this sheet supplies, and it is why
``employee_code`` is the one column that must not be guessed: a wrong code does
not show up as an error, it shows up as somebody else's attendance.

**What it has to reconstruct.** The new sheet is three tiers -- Management (2
people) -> HOD (11) -> staff. The live directory is four: it has a supervisor
layer, 48 people, between the HODs and the workers. Rebuilding straight from
the sheet would flatten those 48 teams onto their HOD and lose real structure
that nobody wrote down anywhere else.

So the import snapshots the existing tree first and splices the supervisors back
in -- ``staff -> L1 -> HOD`` -- wherever it can do so *safely*. Three guards, and
each one reports rather than guesses:

* **The sheet wins on disagreement.** If the old supervisor and the worker land
  under different HODs in the new sheet, the old link is dropped. The sheet is
  newer than the tree.
* **Ambiguous names are never matched.** Twelve names occur on more than one
  employee row in the live directory. Attaching by name there would put
  somebody under a stranger, so those links are dropped and listed.
* **Spelling drift is normalised, not assumed.** The two sheets disagree about
  honorifics -- ``Arvinder Veer Ji`` became ``Arvinder Veerji``, ``Kulbir
  Singh`` became ``Kulbeer Veerji``. ``veer ji``/``veerji`` is folded together;
  anything else that fails to match is reported by name rather than quietly
  dropped.

Nothing is guessed silently, and nothing is written without ``--commit``.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from employee_hierarchy import hierarchy
from employee_hierarchy.constants import (
    AuditAction,
    EmploymentStatus,
    HistoryEvent,
    RecordStatus,
)
from employee_hierarchy.models import (
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    SalaryRevision,
)

SHEET = "Sheet1"

#: Columns, as the workbook spells them.
COL_MANAGEMENT = "Management"
COL_CODE = "JWPL"
COL_NAME = "Name"
COL_DEPARTMENT = "Department"
COL_DESIGNATION = "Designation"
COL_SUB_DEPARTMENT = "Sub Department"
COL_SAP = "SAP"
COL_HOD = "HOD"

#: Values that appear in a person column but are not a person. Same list as the
#: other importer, which earned every entry on it.
NOT_PEOPLE = {"vacant", "193", "bagha purana", ".", "-", "na", "n/a", "nil", "none"}

#: Values sitting in the JWPL column that are not an employee code. ``New Sep``
#: is HR's note that the person joined in September and has not been enrolled on
#: the punching machine yet -- it is a to-do, and it appears on more than one
#: row, so it can never be an identity.
NOT_CODES = {"new sep", "new", "na", "n/a", "-", ".", "nil", "none", "pending"}

#: A real employee code. ``JWPL0593`` is the normal shape; ``TP108`` is a
#: contract worker, and those punch too (62 of the 66 on this sheet punched in
#: the last 30 days), so they are codes like any other.
CODE_PATTERN = re.compile(r"^(JWPL|TP)\d+$", re.IGNORECASE)

#: Prefix for people the sheet names without a code. They are real employees and
#: must appear in the directory, but they can never match a punch record, so the
#: code says so rather than inventing a plausible-looking JWPL number.
NOCODE_PREFIX = "NOCODE"

#: The SAP column's vocabulary, case-folded to one spelling. The sheet carries
#: both ``BEV`` and ``Bev``.
SAP_SEGMENTS = {
    "oil": "Oil",
    "mart": "Mart",
    "bev": "Bev",
    "beverages": "Bev",
    "water": "Water",
    "common": "Common",
    "construction": "Construction",
}

#: Honorific spellings that mean the same man. Applied only when matching a name
#: against the *previous* directory, never when creating someone.
HONORIFIC = re.compile(r"\bveer\s*ji\b", re.IGNORECASE)

DEFAULT_LEVEL = 5
#: Ladder rungs for the two tiers the sheet names without a designation.
MANAGEMENT_LEVEL = 1
HOD_LEVEL = 2


def clean(value):
    """Collapse whitespace; ``None`` becomes empty."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def key_of(name):
    """The identity of a person: case- and spacing-insensitive."""
    return clean(name).casefold()


def match_key(name):
    """Identity for matching against the *old* directory, honorifics folded.

    ``Arvinder Veer Ji`` and ``Arvinder Veerji`` are one man. Used only to line
    the new sheet up with what is already in the database -- creating a person
    always uses the sheet's own spelling.
    """
    return HONORIFIC.sub("veerji", clean(name)).casefold()


def is_person(name):
    return bool(clean(name)) and key_of(name) not in NOT_PEOPLE


def normalise_code(raw):
    """The employee code, or ``""`` if the cell holds something else."""
    value = clean(raw).upper()
    if not value or value.casefold() in NOT_CODES:
        return ""
    return value


def slug(text, used, limit=30):
    """A short unique CODE from a name: ``Maintenance(All Plant)`` -> ``MAINTENANCE_ALL_PLANT``."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", clean(text)).strip("_").upper()[:limit] or "X"
    candidate = base
    counter = 2
    while candidate in used:
        suffix = f"_{counter}"
        candidate = base[: limit - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


def split_name(full_name):
    """``"Vishal Tyagi"`` -> ``("Vishal", "Tyagi")``; a single word has no surname."""
    parts = clean(full_name).split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


class Command(BaseCommand):
    help = "Import the JWPL-coded hierarchy workbook, preserving the supervisor layer."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx workbook.")
        parser.add_argument("--company", default="JIVO_OIL", help="Company code to import into.")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without it the command reports what it would do and stops.",
        )
        parser.add_argument("--backup", help="Write the rows being replaced to this JSON file.")
        parser.add_argument("--report", help="Write the full decision report to this file.")
        parser.add_argument(
            "--no-preserve-supervisors",
            action="store_true",
            help="Build the sheet's three tiers exactly, without splicing the old L1 layer back in.",
        )

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

    def build_plan(self, rows, notes):
        """Turn the sheet into staff, managers, departments and designations.

        Returns ``(staff, managers)``. ``staff`` is one entry per employee row.
        ``managers`` are the people named only in the Management and HOD
        columns -- 7 of the 11 HODs and both Management names have no row of
        their own, and they still have to exist for anyone to report to them.
        """
        staff = []
        managers: dict[str, dict] = {}
        seen_codes: dict[str, int] = {}

        def manager_entry(name, tier):
            entry = managers.setdefault(
                key_of(name),
                {"name": clean(name), "tier": tier, "above": Counter(), "departments": Counter()},
            )
            # Most senior tier they appear in wins: somebody named in both
            # columns is Management, not an HOD.
            entry["tier"] = min(entry["tier"], tier)
            return entry

        nocode_counter = 0
        for row in rows:
            excel_row = row["_excel_row"]
            name = clean(row.get(COL_NAME))
            management = clean(row.get(COL_MANAGEMENT))
            hod = clean(row.get(COL_HOD))
            department = clean(row.get(COL_DEPARTMENT))
            sub_department = clean(row.get(COL_SUB_DEPARTMENT))

            if is_person(management):
                manager_entry(management, MANAGEMENT_LEVEL)
            if is_person(hod):
                entry = manager_entry(hod, HOD_LEVEL)
                if is_person(management):
                    entry["above"][clean(management)] += 1
                if department:
                    entry["departments"][department] += 1
            elif clean(hod):
                notes["not a person"].append(
                    f"row {excel_row}: HOD {clean(hod)!r} is not a name — "
                    f"anyone under it goes to {management or 'the top'}"
                )

            if not is_person(name):
                notes["rows with no employee on them"].append(
                    f"row {excel_row}: {department or 'no department'} / "
                    f"{sub_department or 'no sub-department'} — no employee created"
                )
                continue

            code = normalise_code(row.get(COL_CODE))
            raw_code = clean(row.get(COL_CODE))
            if not code:
                nocode_counter += 1
                code = f"{NOCODE_PREFIX}-{nocode_counter:04d}"
                notes["no usable employee code — cannot be matched to punch data"].append(
                    f"row {excel_row}: {name} — JWPL cell {raw_code or 'blank'!r}, "
                    f"imported as {code}"
                )
            elif not CODE_PATTERN.match(code):
                notes["employee code in an unexpected shape — kept as-is"].append(
                    f"row {excel_row}: {name} — {code!r}"
                )

            if code in seen_codes:
                notes["duplicate employee code — second row imported under a suffix"].append(
                    f"row {excel_row}: {name} reuses {code} (first seen on row {seen_codes[code]})"
                )
                code = f"{code}-DUP{excel_row}"
            else:
                seen_codes[code] = excel_row

            sap_raw = clean(row.get(COL_SAP))
            sap = SAP_SEGMENTS.get(sap_raw.casefold(), "")
            if sap_raw and not sap:
                notes["unrecognised SAP segment — left blank"].append(
                    f"row {excel_row}: {name} — {sap_raw!r}"
                )

            above = hod if is_person(hod) else (management if is_person(management) else "")
            if not above:
                notes["nobody named above them — placed at the top"].append(
                    f"row {excel_row}: {name}"
                )

            staff.append(
                {
                    "row": excel_row,
                    "code": code,
                    "name": name,
                    "department": department,
                    "sub_department": sub_department,
                    "designation": clean(row.get(COL_DESIGNATION)),
                    "sap": sap,
                    "above": above,
                    "management": management if is_person(management) else "",
                }
            )

        # An HOD with no Management named anywhere sits at the top.
        for entry in managers.values():
            if entry["tier"] == HOD_LEVEL and not entry["above"]:
                notes["HOD with no Management named — placed at the top"].append(entry["name"])

        return staff, managers

    # -- the supervisor layer --------------------------------------------

    def snapshot_supervisors(self, notes):
        """Read the existing tree's L1 layer before it is replaced.

        Returns ``{match_key(staff name): supervisor name}`` for everyone whose
        current manager is *not* a top-two-tier manager -- i.e. the layer the
        new sheet does not have.

        Names that occur on more than one employee row are excluded outright.
        The live directory has twelve of those, and a name is the only thing
        the two sheets share, so an ambiguous one cannot be resolved -- and
        attaching somebody to the wrong supervisor is worse than attaching them
        to their HOD.
        """
        existing = list(
            Employee.objects.select_related("reporting_manager").values(
                "full_name", "hierarchy_level", "reporting_manager__full_name",
                "reporting_manager__hierarchy_level",
            )
        )
        if not existing:
            return {}

        counts = Counter(match_key(row["full_name"]) for row in existing)
        ambiguous = {name for name, count in counts.items() if count > 1}
        for name in sorted(ambiguous):
            notes["name on more than one row in the old directory — supervisor not preserved"].append(
                f"{name} ({counts[name]} rows)"
            )

        supervisors = {}
        for row in existing:
            manager = row["reporting_manager__full_name"]
            manager_level = row["reporting_manager__hierarchy_level"]
            if not manager or manager_level is None:
                continue
            # Level 1 is Management and level 2 is an HOD -- the sheet already
            # has both. Only a deeper manager is the layer worth preserving.
            if manager_level < 3:
                continue
            key = match_key(row["full_name"])
            if key in ambiguous or match_key(manager) in ambiguous:
                continue
            supervisors[key] = clean(manager)
        return supervisors

    def apply_supervisors(self, staff, managers, supervisors, notes):
        """Splice the old supervisors back in: ``staff -> L1 -> HOD``.

        Mutates each staff entry's ``above``. A link survives only if the
        supervisor is on the new sheet *and* lands under the same HOD as the
        worker -- otherwise the sheet's placement stands.
        """
        by_key = {match_key(entry["name"]): entry for entry in staff}
        manager_keys = {match_key(entry["name"]) for entry in managers.values()}
        preserved = dropped = 0

        for entry in staff:
            key = match_key(entry["name"])
            supervisor_name = supervisors.get(key)
            if not supervisor_name:
                continue
            supervisor_key = match_key(supervisor_name)
            if supervisor_key == key:
                continue

            supervisor = by_key.get(supervisor_key)
            if supervisor is None:
                if supervisor_key in manager_keys:
                    # They were promoted into the HOD/Management tier on the new
                    # sheet; the sheet's own placement already covers it.
                    continue
                dropped += 1
                notes["supervisor not on the new sheet — worker goes to their HOD"].append(
                    f"{entry['name']} (row {entry['row']}) used to report to {supervisor_name}"
                )
                continue

            if match_key(supervisor["above"]) != match_key(entry["above"]):
                dropped += 1
                notes["old supervisor sits under a different HOD now — sheet wins"].append(
                    f"{entry['name']} (row {entry['row']}) used to report to {supervisor_name}, "
                    f"who is now under {supervisor['above'] or 'nobody'} "
                    f"while {entry['name']} is under {entry['above'] or 'nobody'}"
                )
                continue

            entry["above"] = supervisor["name"]
            entry["is_under_supervisor"] = True
            preserved += 1

        return preserved, dropped

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
            payload[model.__name__] = json.loads(
                json.dumps(list(model.objects.values()), default=str)
            )
        if backup_path:
            with open(backup_path, "w") as handle:
                json.dump(payload, handle, indent=1, default=str)
            self.stdout.write(f"  backup written to {backup_path}")

        counts = {}
        for model in (SalaryRevision, EmployeeSalary, EmployeeHistory, EmployeeAuditLog):
            counts[model.__name__] = model.objects.all().delete()[0]
        # Employees point at each other and at departments; clear the pointers
        # before deleting so PROTECT never fires.
        Employee.objects.update(reporting_manager=None, department=None, designation=None)
        Department.objects.update(head=None, parent=None)
        counts["Employee"] = Employee.objects.all().delete()[0]
        counts["Department"] = Department.objects.all().delete()[0]
        counts["Designation"] = Designation.objects.all().delete()[0]
        return counts

    def write(self, company, staff, managers, notes):
        """Create departments, designations and every person, then wire the tree."""
        used_department_codes = set()
        used_designation_codes = set()

        # --- departments: the Department column is the parent, Sub Department
        # the child. Names are unique per company, and seven sub-departments
        # repeat their parent's name, so a child that collides is qualified.
        parents = {}
        for name in sorted({entry["department"] for entry in staff if entry["department"]}):
            parents[name] = Department.objects.create(
                company=company, code=slug(name, used_department_codes), name=name,
                status=RecordStatus.ACTIVE,
            )

        children = {}
        for entry in staff:
            parent_name, child_name = entry["department"], entry["sub_department"]
            if not child_name:
                continue
            if (parent_name, child_name) in children:
                continue
            parent = parents.get(parent_name)
            display = child_name
            if child_name in parents or display in {c.name for c in children.values()}:
                display = f"{child_name} ({parent_name})" if parent_name else child_name
            children[(parent_name, child_name)] = Department.objects.create(
                company=company, code=slug(display, used_department_codes), name=display,
                parent=parent, status=RecordStatus.ACTIVE,
            )

        def department_for(entry):
            key = (entry["department"], entry["sub_department"])
            if key in children:
                return children[key]
            return parents.get(entry["department"])

        # --- designations. The sheet carries no ladder, so every rung from an
        # employee row gets the same default level; the two manager tiers get
        # theirs from the tier itself. HR corrects these afterwards, and the
        # report says so.
        designations = {}
        for name in sorted({entry["designation"] for entry in staff if entry["designation"]}):
            designations[name] = Designation.objects.create(
                company=company, code=slug(name, used_designation_codes), name=name,
                level=DEFAULT_LEVEL, status=RecordStatus.ACTIVE,
            )
        for tier, label in ((MANAGEMENT_LEVEL, "Management"), (HOD_LEVEL, "Head of Department")):
            designations[label] = Designation.objects.create(
                company=company, code=slug(label, used_designation_codes), name=label,
                level=tier, is_managerial=True, status=RecordStatus.ACTIVE,
            )

        # --- people. Managers first, so staff have somebody to point at.
        by_key: dict[str, Employee] = {}
        manager_sequence = 0
        for entry in sorted(managers.values(), key=lambda item: (item["tier"], item["name"])):
            manager_sequence += 1
            first, last = split_name(entry["name"])
            label = "Management" if entry["tier"] == MANAGEMENT_LEVEL else "Head of Department"
            employee = Employee.objects.create(
                company=company,
                employee_code=f"MGR-{manager_sequence:03d}",
                first_name=first,
                last_name=last,
                designation=designations[label],
                job_title=label,
                employment_status=EmploymentStatus.ACTIVE,
                is_manager=True,
            )
            by_key[key_of(entry["name"])] = employee

        for entry in staff:
            first, last = split_name(entry["name"])
            employee = Employee.objects.create(
                company=company,
                employee_code=entry["code"],
                first_name=first,
                last_name=last,
                department=department_for(entry),
                designation=designations.get(entry["designation"]),
                job_title=entry["designation"],
                sap_segment=entry["sap"],
                employment_status=EmploymentStatus.ACTIVE,
            )
            # A staff row's identity is the row, never the name -- two workers
            # really are called Gurpreet Singh. The key is only for the people
            # named above them, and a manager already holds that key.
            by_key.setdefault(key_of(entry["name"]), employee)
            entry["employee"] = employee

        # --- the tree. Managers are placed first so that a staff member whose
        # supervisor is another staff member finds a placed manager.
        for entry in sorted(managers.values(), key=lambda item: item["tier"]):
            employee = by_key[key_of(entry["name"])]
            above = entry["above"].most_common(1)
            manager = by_key.get(key_of(above[0][0])) if above else None
            hierarchy.place(employee, manager)
            if above and len(entry["above"]) > 1:
                notes["manager named under more than one superior"].append(
                    f"{entry['name']}: "
                    f"{', '.join(f'{n} x{c}' for n, c in entry['above'].most_common())} "
                    f"— used {above[0][0]}"
                )

        # Staff whose supervisor is another staff member have to wait until that
        # supervisor is placed, or their path is built from an unplaced parent.
        # Ordering by depth-of-manager does that in one pass.
        def placement_order(entry):
            return 0 if key_of(entry["above"]) in managers else 1

        for entry in sorted(staff, key=placement_order):
            employee = entry["employee"]
            manager = by_key.get(key_of(entry["above"]))
            if manager is not None and manager.pk == employee.pk:
                manager = None
            hierarchy.place(employee, manager)
            if manager is not None and not manager.is_manager:
                Employee.objects.filter(pk=manager.pk).update(is_manager=True)

        # --- department heads, where the sheet names one.
        for entry in managers.values():
            employee = by_key[key_of(entry["name"])]
            for name, _count in entry["departments"].most_common(1):
                department = parents.get(name)
                if department is not None and department.head_id is None:
                    department.head = employee
                    department.save(update_fields=["head"])

        # --- the trail. One history row per person saying where they came
        # from, so the timeline does not start blank, and one audit row
        # recording the import as an administrative act.
        EmployeeHistory.objects.bulk_create(
            [
                EmployeeHistory(
                    employee=entry["employee"],
                    event=HistoryEvent.JOINED,
                    to_value=entry["designation"] or "Imported",
                    notes=(
                        f"Imported from the JWPL hierarchy workbook, sheet row {entry['row']}."
                        + (
                            " Supervisor preserved from the previous directory."
                            if entry.get("is_under_supervisor")
                            else ""
                        )
                    ),
                )
                for entry in staff
            ]
        )
        EmployeeAuditLog.objects.bulk_create(
            [
                EmployeeAuditLog(
                    employee=entry["employee"],
                    action=AuditAction.EMPLOYEE_CREATED,
                    field="employee_code",
                    new_value=entry["code"],
                    reason="JWPL hierarchy workbook import",
                )
                for entry in staff
            ]
        )

        return {
            "departments": len(parents) + len(children),
            "designations": len(designations),
            "managers": len(managers),
            "staff": len(staff),
        }

    # -- reporting -------------------------------------------------------

    def render_report(self, staff, managers, notes, preserved, dropped, counts=None):
        lines = []
        add = lines.append
        add("JWPL hierarchy import — plan")
        add(f"  employee rows              : {len(staff)}")
        add(f"  managers named above them  : {len(managers)}")
        codes = [entry["code"] for entry in staff]
        add(f"  with a real employee code  : {sum(1 for c in codes if not c.startswith(NOCODE_PREFIX))}")
        add(f"  without one (unmatchable)  : {sum(1 for c in codes if c.startswith(NOCODE_PREFIX))}")
        add(f"  departments                : {len({e['department'] for e in staff if e['department']})}")
        add(f"  sub-departments            : {len({(e['department'], e['sub_department']) for e in staff if e['sub_department']})}")
        add(f"  designations               : {len({e['designation'] for e in staff if e['designation']})}")
        add("")
        add("Supervisor layer carried over from the old directory")
        add(f"  preserved                  : {preserved}")
        add(f"  dropped (reported below)   : {dropped}")
        if counts:
            add("")
            add("Rows replaced")
            for name, count in counts.items():
                add(f"  {name:<20} : {count}")
        add("")
        add("Judgements made — each of these is worth a human eye")
        if not notes:
            add("  (none)")
        for heading in sorted(notes):
            entries = notes[heading]
            add("")
            add(f"  {heading} ({len(entries)}):")
            for entry in entries:
                add(f"    - {entry}")
        add("")
        add("Designation levels are a placeholder — the sheet carries no ladder.")
        return "\n".join(lines)

    # -- entry point -----------------------------------------------------

    def handle(self, *args, **options):
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise SystemExit(f"No company with code {options['company']}.")

        notes = defaultdict(list)
        rows = self.read_rows(options["file"])
        staff, managers = self.build_plan(rows, notes)

        preserved = dropped = 0
        if not options["no_preserve_supervisors"]:
            supervisors = self.snapshot_supervisors(notes)
            preserved, dropped = self.apply_supervisors(staff, managers, supervisors, notes)

        if not options["commit"]:
            report = self.render_report(staff, managers, notes, preserved, dropped)
            self.stdout.write(report)
            if options["report"]:
                with open(options["report"], "w") as handle:
                    handle.write(report)
            self.stdout.write(self.style.WARNING("\nDry run — nothing written. Pass --commit to apply."))
            return

        with transaction.atomic():
            counts = self.wipe(options["backup"])
            written = self.write(company, staff, managers, notes)

        report = self.render_report(staff, managers, notes, preserved, dropped, counts)
        self.stdout.write(report)
        if options["report"]:
            with open(options["report"], "w") as handle:
                handle.write(report)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nWritten: {written['staff']} employees, {written['managers']} managers, "
                f"{written['departments']} departments, {written['designations']} designations."
            )
        )
