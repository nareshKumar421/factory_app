"""
Read the SAP column off the hierarchy workbook and file each employee under it.

The ``SAP`` column on ``Sheet1`` is the branch: ``Oil``, ``Bev``, ``Mart``,
``Water``, ``Construction``, ``Common``. It is the same value
``import_hierarchy_jwpl`` writes to :attr:`Employee.sap_segment`, so this
command is the second half of that import -- the segment says where somebody is
costed, the branch is the master HR picks from, and they are kept as two fields
because one is SAP's word and the other is ours.

**The spelling is normalised, and that is the point of a master.** The sheet
carries ``BEV`` 22 times and ``Bev`` 15 times for one branch; matched
case-insensitively they are one row, and left alone they would have been two
branches that no report could add together. Everything is compared
case-insensitively and stored title-cased, which is the form
``sap_segment`` already uses.

Employees are matched on ``employee_code`` against the sheet's ``JWPL`` column
-- the same join key the punching machines use -- because a code is exact and a
name is not. ``--match-by-name`` additionally places the handful of sheet rows
whose code cell is blank or reads ``New Sep``, and it only ever acts on a name
the directory holds **once**; an ambiguous name is reported, never guessed.

Anybody absent from the sheet is left exactly as they are. Their branch is not
cleared and not defaulted -- the sheet not mentioning somebody says nothing
about where they work, and a blank branch is visibly unanswered where a wrong
one is not.

Dry run by default, like the other repairs here::

    # look first -- writes nothing
    python manage.py import_employee_branches --file "Factory New Heirarchy.xlsx"

    # do it
    python manage.py import_employee_branches --file "Factory New Heirarchy.xlsx" \
        --match-by-name --commit

Safe to re-run: it reuses branches that already exist and only writes an
employee whose branch is actually changing.
"""

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company

from ...constants import RecordStatus
from ...models import Branch, Employee
from ...services import make_default_branch

#: Code cells that mean "no code", not a code.
NOT_A_CODE = {"", "NEW SEP", "NONE", "NA", "N/A", "-"}

#: How far each way of identifying somebody is trusted. A code is exact; a name
#: is a guess that happened to be unique; a segment is what we already held.
#: A weaker source must never overwrite a stronger one -- the sheet carries
#: codeless rows whose name also belongs to somebody the sheet placed by code,
#: and letting the name win filed that person under a branch meant for nobody.
PRECEDENCE = {"code": 3, "name": 2, "segment": 1}


def canonical(value):
    """One spelling per branch. ``'BEV'`` and ``'Bev'`` are the same branch."""
    return " ".join(str(value or "").split()).title()


class Command(BaseCommand):
    help = "File employees under the branch named in the workbook's SAP column."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx workbook.")
        parser.add_argument("--sheet", default="Sheet1", help="Sheet name. Default: Sheet1.")
        parser.add_argument(
            "--company", default="JIVO_OIL", help="Company code the directory lives in."
        )
        parser.add_argument(
            "--default",
            dest="default_code",
            default=None,
            help=(
                "Branch code to make the default. Left out, Oil is used when the sheet "
                "has it and the default is otherwise left alone. Named explicitly and "
                "absent, the run is refused rather than picking something else."
            ),
        )
        parser.add_argument(
            "--match-by-name",
            action="store_true",
            help="Also place sheet rows with no code, when the name is unique in the directory.",
        )
        parser.add_argument(
            "--fill-from-sap-segment",
            action="store_true",
            help=(
                "Place employees the sheet does not mention using the sap_segment "
                "already on their record. Same source, already imported."
            ),
        )
        parser.add_argument(
            "--drop-unused",
            action="store_true",
            help=(
                "Delete branches this sheet does not name. Anybody still filed under "
                "one has their branch CLEARED first -- nothing is invented for them."
            ),
        )
        parser.add_argument("--commit", action="store_true", help="Actually write.")

    # -- reading -----------------------------------------------------------

    def _rows(self, path, sheet):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - environment problem
            raise CommandError("openpyxl is needed to read the workbook.") from exc
        try:
            workbook = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError as exc:
            raise CommandError(f"No workbook at {path!r}.") from exc
        if sheet not in workbook.sheetnames:
            raise CommandError(
                f"{path!r} has no sheet {sheet!r}. It has: {', '.join(workbook.sheetnames)}."
            )
        rows = list(workbook[sheet].iter_rows(values_only=True))
        if not rows:
            raise CommandError(f"Sheet {sheet!r} is empty.")

        header = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
        for column in ("SAP", "JWPL", "Name"):
            if column not in header:
                raise CommandError(
                    f"Sheet {sheet!r} has no {column!r} column. Found: {', '.join(header)}."
                )
        sap, code, name = (header.index(c) for c in ("SAP", "JWPL", "Name"))
        body = [row for row in rows[1:] if any(cell is not None for cell in row)]
        return body, sap, code, name

    # -- doing -------------------------------------------------------------

    def handle(self, *args, **options):
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {options['company']!r}.")

        body, i_sap, i_code, i_name = self._rows(options["file"], options["sheet"])

        employees = list(Employee.objects.filter(company=company))
        by_pk_all = {e.pk: e for e in employees}
        by_code = {e.employee_code.strip().upper(): e for e in employees}
        by_name = defaultdict(list)
        for employee in employees:
            by_name[employee.full_name.strip().upper()].append(employee)

        # --- the branches the sheet asks for ------------------------------
        wanted = {}
        for row in body:
            label = canonical(row[i_sap])
            if label:
                wanted.setdefault(label.upper(), label)

        existing = {b.code.strip().upper(): b for b in Branch.objects.filter(company=company)}
        existing.update({b.name.strip().upper(): b for b in Branch.objects.filter(company=company)})

        branches, created_names = {}, []
        for key, label in sorted(wanted.items()):
            branch = existing.get(key)
            if branch is None:
                branch = Branch(
                    company=company,
                    code=label.upper()[:30],
                    name=label[:100],
                    description="From the SAP column of the hierarchy workbook.",
                    status=RecordStatus.ACTIVE,
                )
                created_names.append(label)
            branches[key] = branch

        # --- who goes where ------------------------------------------------
        claims = defaultdict(list)  # employee pk -> [(branch key, source)]
        plan = {}            # employee pk -> branch key
        missing_code = []    # sheet code not in the directory
        ambiguous = []       # blank code, name held more than once
        absent = []          # blank code, name not in the directory
        no_branch = []       # a row with no SAP value at all

        for row in body:
            label = canonical(row[i_sap])
            raw = str(row[i_code]).strip().upper() if row[i_code] is not None else ""
            person = str(row[i_name]).strip() if row[i_name] else ""
            if not label:
                no_branch.append(person or raw)
                continue

            if raw not in NOT_A_CODE:
                employee = by_code.get(raw)
                if employee is None:
                    missing_code.append(f"{raw} ({person})")
                    continue
                claims[employee.pk].append((label.upper(), "code"))
                continue

            if not options["match_by_name"]:
                absent.append(person)
                continue
            candidates = by_name.get(person.upper(), [])
            if len(candidates) == 1:
                claims[candidates[0].pk].append((label.upper(), "name"))
            elif len(candidates) > 1:
                ambiguous.append(f"{person} (x{len(candidates)})")
            else:
                absent.append(person)

        # The segment is the same SAP value, imported earlier by
        # import_hierarchy_jwpl. Using it places people the sheet forgot without
        # guessing anything the business has not already said.
        if options["fill_from_sap_segment"]:
            for employee in employees:
                if employee.pk in claims:
                    continue
                label = canonical(employee.sap_segment)
                if not label:
                    continue
                key = label.upper()
                if key not in branches:
                    branches[key] = existing.get(key) or Branch(
                        company=company,
                        code=label.upper()[:30],
                        name=label[:100],
                        description="From an employee's SAP segment.",
                        status=RecordStatus.ACTIVE,
                    )
                    if branches[key].pk is None:
                        created_names.append(label)
                    wanted.setdefault(key, label)
                claims[employee.pk].append((key, "segment"))

        # One answer per employee. The strongest source wins outright; two
        # claims of the *same* strength that disagree are a contradiction in the
        # sheet -- almost always one code typed against two people -- and that is
        # reported, never resolved by whichever row happened to be last.
        conflicts = []
        for pk, entries in claims.items():
            best = max(PRECEDENCE[source] for _, source in entries)
            labels = {label for label, source in entries if PRECEDENCE[source] == best}
            if len(labels) > 1:
                employee = by_pk_all[pk]
                conflicts.append(
                    f"{employee.employee_code} ({employee.full_name}): "
                    + " vs ".join(sorted(wanted.get(l, l) for l in labels))
                )
                continue
            plan[pk] = labels.pop()

        from_segment = sum(
            1
            for pk, key in plan.items()
            if all(s == "segment" for _, s in claims[pk])
        )

        # --- report ---------------------------------------------------------
        spread = Counter(plan.values())
        self.stdout.write(f"Workbook : {options['file']} [{options['sheet']}], {len(body)} rows")
        self.stdout.write(f"Company  : {company.code}\n")
        self.stdout.write("Branches:")
        for key in sorted(wanted):
            mark = "new" if wanted[key] in created_names else "exists"
            self.stdout.write(f"   {wanted[key]:<14} {mark:<7} {spread.get(key, 0):>4} employee(s)")

        by_pk = {e.pk: e for e in employees}
        # A branch that does not exist yet has no pk, and nobody can already be
        # filed under it -- so those are all changes. Comparing against a None
        # pk counted them as "no change" and reported one write instead of 236.
        changing = [
            pk
            for pk, key in plan.items()
            if branches[key].pk is None or by_pk[pk].branch_id != branches[key].pk
        ]
        from_sheet = len(plan) - from_segment
        self.stdout.write(
            f"\nFrom sheet: {from_sheet} of {len(body)} rows placed "
            f"({len(changing)} employee(s) change branch in total)"
        )
        if options["fill_from_sap_segment"]:
            self.stdout.write(f"From segment: {from_segment} employee(s) placed by their sap_segment")
        untouched = [e for e in employees if e.pk not in plan]
        self.stdout.write(
            f"Untouched: {len(untouched)} employee(s) the sheet and the segment cannot place"
        )

        # Branches this sheet does not name. They are the leftovers -- the
        # placeholder the seed migration created, or a branch the business has
        # stopped using.
        stale = [
            b
            for b in Branch.objects.filter(company=company)
            if b.code.strip().upper() not in wanted and b.name.strip().upper() not in wanted
        ]
        if stale:
            holding = {
                b.pk: [e for e in untouched if e.branch_id == b.pk] for b in stale
            }
            verb = "will be deleted" if options["drop_unused"] else "not named by the sheet"
            for branch in stale:
                stuck = len(holding[branch.pk])
                note = f", clearing {stuck} employee(s) first" if (stuck and options["drop_unused"]) else (
                    f", still holding {stuck} employee(s)" if stuck else ""
                )
                self.stdout.write(
                    self.style.WARNING(f"Stale    : {branch.name} {verb}{note}")
                )

        for title, entries in (
            ("Code not in the directory", missing_code),
            ("No code on the sheet", absent),
            ("Name held more than once", ambiguous),
            ("Row with no SAP value", no_branch),
            ("CONFLICT — one person, two branches", conflicts),
        ):
            if entries:
                shown = ", ".join(entries[:8])
                more = f" … +{len(entries) - 8}" if len(entries) > 8 else ""
                self.stdout.write(self.style.WARNING(f"{title}: {len(entries)} — {shown}{more}"))

        if not options["commit"]:
            self.stdout.write(
                self.style.WARNING("\nDry run. Re-run with --commit to write.")
            )
            return

        # --- write ------------------------------------------------------------
        with transaction.atomic():
            for branch in branches.values():
                if branch.pk is None:
                    branch.save()

            updates = defaultdict(list)
            for pk, key in plan.items():
                updates[branches[key].pk].append(pk)
            written = 0
            for branch_pk, pks in updates.items():
                written += (
                    Employee.objects.filter(pk__in=pks)
                    .exclude(branch_id=branch_pk)
                    .update(branch_id=branch_pk)
                )

            asked = options["default_code"] is not None
            default_code = (options["default_code"] or "OIL").strip().upper()
            if default_code:
                target = branches.get(default_code) or Branch.objects.filter(
                    company=company, code__iexact=default_code
                ).first()
                if target is None and asked:
                    # Named explicitly and not there: refuse, rather than crown
                    # a branch nobody asked for.
                    raise CommandError(
                        f"--default {default_code!r} names no branch. "
                        f"The sheet offers: {', '.join(sorted(wanted))}."
                    )
                if target is None:
                    self.stdout.write(
                        f"Default  : left alone — the sheet has no {default_code} branch."
                    )
                else:
                    make_default_branch(target)
                    self.stdout.write(f"Default  : {target.name}")

            dropped = cleared = 0
            if options["drop_unused"]:
                for branch in stale:
                    # Cleared rather than moved: the sheet does not say where
                    # these people are, and a blank branch is visibly
                    # unanswered where a made-up one is not.
                    cleared += Employee.objects.filter(branch=branch).update(branch=None)
                    branch.refresh_from_db()
                    if branch.is_default:
                        raise CommandError(
                            f"{branch.name} is still the default branch, so it cannot be "
                            "dropped. Pass --default with a branch the sheet names."
                        )
                    branch.delete()
                    dropped += 1

        self.stdout.write(self.style.SUCCESS(f"\n{written} employee(s) re-filed."))
        if options["drop_unused"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{dropped} stale branch(es) deleted, {cleared} employee(s) left unfiled."
                )
            )
