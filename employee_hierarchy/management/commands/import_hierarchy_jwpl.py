"""
Load the JWPL-coded hierarchy workbook, keeping the supervisor layer it lost.

There are two modes, and they differ in how much they are willing to destroy.

**``--update-codes`` -- the narrow one, and the one to reach for.** It changes
two columns, ``employee_code`` and ``sap_segment``, on the employees already in
the directory, matched by name. The reporting tree, salaries, salary revisions,
history, audit trail, user links and primary keys are all left exactly as they
are. This is the mode that answers "the directory has no codes" without betting
anything else on the sheet::

    # look before you leap: prints every change and every problem, writes nothing
    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx" \
        --update-codes

    # do it
    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx" \
        --update-codes --company JIVO_OIL --commit \
        --backup /tmp/codes-before.json --report /tmp/report.txt

    # settle a name the directory holds twice: sheet row 24 is employee 123
    ... --update-codes --resolve 24=123

**The default -- a full rebuild.** It wipes everything this module owns and
builds it again from the sheet, splicing the old supervisor layer back in.
Use it when the *structure* is what is wrong, and read :meth:`Command.wipe`
first: ``EmployeeSalary`` and ``SalaryRevision`` are deleted and **not**
rebuilt, ``Employee.user`` links are dropped, and every primary key changes::

    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx"
    python manage.py import_hierarchy_jwpl --file "Factory New Heirarchy.xlsx" \
        --company JIVO_OIL --commit --backup /tmp/before.json --report /tmp/report.txt

It replaces ``import_factory_hierarchy``, which read a chart-shaped sheet
(``Management | Department | HOD | Sub Department | L1 | Employee Name``) where
managers existed only as names in columns and one person could be spread across
dozens of rows. HR now keeps a flat person-per-row sheet instead::

    S.NO | Management | JWPL | Name | Department | Designation | Sub Department | SAP | HOD

That layout makes most of the old command's judgement apparatus -- majority-vote
superiors, split supervisor cells, per-row staff identity -- unnecessary, which
is why this is a new file rather than a mode flag on the old one. What it does
keep is the hard-won rule underneath all of it: a name in a *staff* column is
never merged with another of the same name. Twelve people disappeared that way
the first time.

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

#: HR's answer to the one ambiguity the sheet cannot settle by itself.
#:
#: A manager named in the HOD column is merged into their own staff row only
#: when the name picks out exactly one -- otherwise the HOD's code could land
#: on a namesake. ``Sandeep Singh`` is on two rows, and the sheet itself says
#: which is which:
#:
#: * row 24 -- JWPL2884, department Audit, **Designation ``HOD``**
#: * row 64 -- JWPL2609, Gupta FG Ecom, Designation ``Fork Lift``, under
#:   Prabhu Veerji
#:
#: So the HOD of 21 people is row 24. Left unmerged he keeps a synthetic
#: ``MGR-nnn`` and no code at all, and his punches match nobody. Confirmed
#: against the Designation column and with HR on 2026-09-17.
#:
#: Keyed by name and pinned to the Excel row rather than the code, so a re-cut
#: sheet whose row 24 is somebody else fails the name check below instead of
#: silently mis-assigning. Re-check this entry whenever HR re-cuts the sheet.
HOD_STAFF_ROW = {
    "sandeep singh": 24,
}

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
        parser.add_argument(
            "--resolve",
            action="append",
            default=[],
            metavar="ROW=EMPLOYEE_ID",
            help=(
                "Settle one ambiguous match by hand, e.g. --resolve 64=123 to say that "
                "sheet row 64 is employee 123. Repeatable. Used by --update-codes for "
                "names that sit on more than one directory row, which nothing in either "
                "sheet can tell apart."
            ),
        )
        parser.add_argument(
            "--update-codes",
            action="store_true",
            help=(
                "Do not rebuild anything. Match the sheet to the employees already "
                "in the directory by name and write only employee_code and "
                "sap_segment onto them. The tree, salaries, history and user links "
                "are left exactly as they are."
            ),
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

        # --- a manager who also has a staff row IS that row.
        #
        # Three of the eleven HODs are named in the HOD column *and* appear in
        # the Name column. Creating both gives two records for one person: a
        # synthetic ``MGR-nnn`` that the team hangs off, and their real row --
        # carrying their real JWPL code -- orphaned at the top with no reports.
        # The code is the join key to the punching machines, so that split puts
        # their attendance on the record that is not their job.
        #
        # The merge is only safe when the name picks out exactly one staff row.
        # ``Sandeep Singh`` is on two, and one of them is a fork-lift operator
        # in Gupta FG Ecom -- merging the HOD into a namesake is the same defect
        # as merging two workers, so an ambiguous name keeps its own record and
        # is reported instead.
        staff_by_key = defaultdict(list)
        for entry in staff:
            staff_by_key[key_of(entry["name"])].append(entry)

        for manager_key, manager in managers.items():
            candidates = staff_by_key.get(manager_key, [])
            if len(candidates) > 1:
                # HR may have said which of the namesakes is the manager.
                chosen_row = HOD_STAFF_ROW.get(manager_key)
                chosen = [c for c in candidates if c["row"] == chosen_row]
                if not chosen:
                    notes["manager shares a name with more than one staff row — left as a separate record"].append(
                        f"{manager['name']}: rows "
                        f"{', '.join(str(c['row']) for c in candidates)} — none merged"
                        + (
                            f" (HR named row {chosen_row}, which is not one of them — "
                            "the sheet has been re-cut; confirm before trusting this)"
                            if chosen_row is not None
                            else ""
                        )
                    )
                    continue
                notes["manager shares a name with more than one staff row — HR named which"].append(
                    f"{manager['name']}: rows "
                    f"{', '.join(str(c['row']) for c in candidates)} — merged into row {chosen_row}"
                )
                candidates = chosen
            if not candidates:
                continue

            row = candidates[0]
            manager["embodied_by"] = row
            row["manager_tier"] = manager["tier"]
            # Their own row is the sheet speaking about them directly, so it
            # wins. It is only when that row names nobody above them that the
            # consensus from the people who call them HOD is worth anything.
            if not row["above"] and manager["above"]:
                row["above"] = manager["above"].most_common(1)[0][0]
            notes["manager merged with their own staff row — one record, their real code"].append(
                f"{manager['name']} — row {row['row']}, code {row['code']}, "
                f"designation {row['designation'] or 'none'}, "
                f"under {row['above'] or 'nobody'}"
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
            if entry.get("embodied_by"):
                # Their staff row is created below and becomes this manager.
                continue
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
            tier = entry.get("manager_tier")
            # The sheet's own designation stays on job_title either way; the
            # designation FK carries the grade, and a merged manager's grade is
            # their tier, not the rung their row happened to name.
            designation = designations.get(entry["designation"])
            if tier is not None:
                designation = designations[
                    "Management" if tier == MANAGEMENT_LEVEL else "Head of Department"
                ]
            employee = Employee.objects.create(
                company=company,
                employee_code=entry["code"],
                first_name=first,
                last_name=last,
                department=department_for(entry),
                designation=designation,
                job_title=entry["designation"],
                sap_segment=entry["sap"],
                employment_status=EmploymentStatus.ACTIVE,
                is_manager=tier is not None,
            )
            # A staff row's identity is the row, never the name -- two workers
            # really are called Gurpreet Singh. The key is only for the people
            # named above them, and a manager already holds that key.
            by_key.setdefault(key_of(entry["name"]), employee)
            entry["employee"] = employee

        # --- the tree. Managers are placed first so that a staff member whose
        # supervisor is another staff member finds a placed manager.
        for entry in sorted(managers.values(), key=lambda item: item["tier"]):
            if entry.get("embodied_by"):
                # Placed with the staff below, from their own row.
                continue
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
            # A merged manager has to be placed before their own team, or the
            # team's path is built from a parent that is not there yet.
            if entry.get("manager_tier") is not None:
                # Senior tier first, so a merged HOD under merged Management
                # still finds their own parent already placed.
                return (-1, entry["manager_tier"])
            return (0 if key_of(entry["above"]) in managers else 1, 0)

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

    # -- code-only update ------------------------------------------------

    def plan_code_update(self, company, staff, notes, resolved=None):
        """Match the sheet onto the employees already in the directory.

        Returns the list of changes to make, one per employee that is going to
        move. Nothing here touches the tree, the salaries, the history or the
        user links -- the directory keeps the structure it has and only gains
        the two columns the punching machines need.

        Every way this can go wrong ends in a note rather than a write. The
        code is the join key to 432,000 punch records, so a row that cannot be
        matched *with certainty* is worth far less than a row matched wrongly:
        a bad code does not surface as an error, it surfaces as somebody else's
        attendance.
        """
        resolved = resolved or {}
        live = list(Employee.objects.filter(company=company))
        by_id = {e.id: e for e in live}
        by_name = defaultdict(list)
        for employee in live:
            by_name[match_key(employee.full_name)].append(employee)

        changes = {}
        claimed_by = {}
        unchanged = 0

        for entry in staff:
            name, code, row = entry["name"], entry["code"], entry["row"]

            # A synthetic NOCODE-nnnn is not an identity -- it is the sheet
            # admitting it has none. Writing it over whatever the directory
            # holds would destroy a real code and still match no punch, so
            # these are reported and skipped.
            if code.startswith(NOCODE_PREFIX):
                notes["sheet has no code for them — directory left untouched"].append(
                    f"row {row}: {name}"
                )
                continue

            # An explicit ROW=EMPLOYEE_ID overrides the name match entirely --
            # it is the only way to settle a name the directory holds twice.
            if row in resolved:
                chosen = by_id.get(resolved[row])
                if chosen is None:
                    notes["--resolve names an employee that is not in this company"].append(
                        f"row {row}: {name} — no employee {resolved[row]}"
                    )
                    continue
                notes["ambiguity settled by hand"].append(
                    f"row {row}: {name} — employee {chosen.id} "
                    f"({chosen.full_name}), code {code}"
                )
                matches = [chosen]
            else:
                matches = by_name.get(match_key(name), [])
            if not matches:
                notes["on the sheet, not in the directory — no row to update"].append(
                    f"row {row}: {name} — code {code} goes nowhere"
                )
                continue
            if len(matches) > 1:
                notes["name is on more than one directory row — skipped, needs HR"].append(
                    f"row {row}: {name} — employees "
                    f"{', '.join(str(m.id) for m in matches)} — code {code} not written"
                )
                continue

            employee = matches[0]

            # Two sheet rows pointing at one person: the second is a different
            # human the directory has never heard of, not a correction.
            if employee.id in claimed_by:
                notes["two sheet rows match the same person — neither written"].append(
                    f"rows {claimed_by[employee.id]} and {row}: {name} (employee {employee.id})"
                )
                changes.pop(employee.id, None)
                continue
            claimed_by[employee.id] = row

            sap = entry["sap"] or employee.sap_segment
            if employee.employee_code == code and employee.sap_segment == sap:
                unchanged += 1
                continue

            changes[employee.id] = {
                "employee": employee,
                "row": row,
                "name": employee.full_name,
                "sheet_name": name,
                "from_code": employee.employee_code,
                "to_code": code,
                "from_sap": employee.sap_segment,
                "to_sap": sap,
            }

        # --- collisions, judged against the state the update ENDS in.
        #
        # Checking "is this code taken right now?" would reject the legitimate
        # cases: the directory's codes are being reshuffled wholesale, so most
        # targets are occupied at the moment they are asked for, and two people
        # swapping codes is a straight cycle. A code is only really taken if
        # whoever holds it today is still holding it when the update finishes.
        staying = {
            employee.employee_code: employee
            for employee in live
            if employee.id not in changes
        }
        for employee_id, change in list(changes.items()):
            blocker = staying.get(change["to_code"])
            if blocker is not None and blocker.id != employee_id:
                notes["code already belongs to somebody else — skipped"].append(
                    f"row {change['row']}: {change['sheet_name']} (employee {employee_id}) "
                    f"wants {change['to_code']}, held by {blocker.full_name} "
                    f"(employee {blocker.id}), who is not moving"
                )
                changes.pop(employee_id)
                # They keep the code they had, so they now block others too.
                staying[change["from_code"]] = change["employee"]

        matched_ids = set(claimed_by)
        for employee in live:
            if employee.id not in matched_ids:
                notes["in the directory, not on the sheet — left as they are"].append(
                    f"employee {employee.id}: {employee.full_name} ({employee.employee_code})"
                )

        return list(changes.values()), unchanged, len(live)

    def apply_code_update(self, changes, backup_path):
        """Write the codes, in an order no unique constraint can trip over.

        ``(company, employee_code)`` is unique, so a straight row-by-row update
        deadlocks on any cycle -- two people swapping codes, or A taking the
        code B is about to leave. Every code is therefore parked on a temporary
        value first and settled afterwards; two passes always terminate, where
        ordering heuristics do not.
        """
        if backup_path:
            payload = [
                {
                    "employee_id": change["employee"].id,
                    "full_name": change["name"],
                    "employee_code": change["from_code"],
                    "sap_segment": change["from_sap"],
                }
                for change in changes
            ]
            with open(backup_path, "w") as handle:
                json.dump(payload, handle, indent=1, default=str)
            self.stdout.write(f"  backup written to {backup_path}")

        for index, change in enumerate(changes):
            Employee.objects.filter(pk=change["employee"].pk).update(
                employee_code=f"TMP-{index:05d}-{change['employee'].pk}"
            )

        for change in changes:
            employee = change["employee"]
            employee.employee_code = change["to_code"]
            employee.sap_segment = change["to_sap"]
            employee.save(update_fields=["employee_code", "sap_segment", "updated_at"])

        # The code is identity-critical, so each move is an administrative act
        # somebody may have to account for later.
        EmployeeAuditLog.objects.bulk_create(
            [
                EmployeeAuditLog(
                    employee=change["employee"],
                    action=AuditAction.EMPLOYEE_UPDATED,
                    field="employee_code",
                    previous_value=change["from_code"],
                    new_value=change["to_code"],
                    reason="JWPL hierarchy workbook import",
                    notes=f"Sheet row {change['row']}, name on sheet {change['sheet_name']!r}.",
                )
                for change in changes
                if change["from_code"] != change["to_code"]
            ]
        )
        return len(changes)

    def render_code_report(self, changes, unchanged, live_total, notes, committed):
        lines = ["JWPL code update — %s" % ("written" if committed else "plan")]
        lines.append(f"  employees in the directory : {live_total}")
        lines.append(f"  codes to write             : {len(changes)}")
        lines.append(f"  already correct            : {unchanged}")
        lines.append("")
        if changes:
            lines.append("Codes being written")
            for change in sorted(changes, key=lambda c: c["row"]):
                sap = ""
                if change["from_sap"] != change["to_sap"]:
                    sap = f", sap {change['from_sap'] or 'blank'} -> {change['to_sap']}"
                lines.append(
                    f"    - employee {change['employee'].id} {change['name']}: "
                    f"{change['from_code']} -> {change['to_code']}{sap}"
                )
            lines.append("")
        lines.append("Judgements made — each of these is worth a human eye")
        lines.append("")
        for heading in sorted(notes):
            entries = notes[heading]
            lines.append(f"  {heading} ({len(entries)}):")
            for entry in entries:
                lines.append(f"    - {entry}")
            lines.append("")
        return "\n".join(lines)

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

        if options["update_codes"]:
            return self.handle_code_update(company, staff, notes, options)

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

    def handle_code_update(self, company, staff, notes, options):
        """``--update-codes``: write the two columns, change nothing else."""
        # The rebuild's notes are about a tree this mode is not going to touch,
        # so they would only be noise in the report. Keep the ones that are
        # about the codes themselves.
        keep = {
            "no usable employee code — cannot be matched to punch data",
            "duplicate employee code — second row imported under a suffix",
            "employee code in an unexpected shape — kept as-is",
            "unrecognised SAP segment — left blank",
            "manager shares a name with more than one staff row — HR named which",
        }
        notes = defaultdict(list, {k: v for k, v in notes.items() if k in keep})

        resolved = {}
        for item in options["resolve"]:
            row, _, employee_id = item.partition("=")
            if not employee_id.strip().isdigit() or not row.strip().isdigit():
                raise SystemExit(f"--resolve wants ROW=EMPLOYEE_ID, got {item!r}.")
            resolved[int(row)] = int(employee_id)

        changes, unchanged, live_total = self.plan_code_update(
            company, staff, notes, resolved
        )

        if not options["commit"]:
            report = self.render_code_report(changes, unchanged, live_total, notes, False)
            self.stdout.write(report)
            if options["report"]:
                with open(options["report"], "w") as handle:
                    handle.write(report)
            self.stdout.write(
                self.style.WARNING("\nDry run — nothing written. Pass --commit to apply.")
            )
            return

        with transaction.atomic():
            written = self.apply_code_update(changes, options["backup"])

        report = self.render_code_report(changes, unchanged, live_total, notes, True)
        self.stdout.write(report)
        if options["report"]:
            with open(options["report"], "w") as handle:
                handle.write(report)
        self.stdout.write(
            self.style.SUCCESS(f"\nWritten: {written} employee codes updated.")
        )
