"""
Fill what the JWPL sheet could not say, from the chart-shaped workbook.

    # look before you leap: prints every change and every problem, writes nothing
    python manage.py fill_hierarchy_gaps --file "Factory Heirarchy (2).xlsx"

    # do it
    python manage.py fill_hierarchy_gaps --file "Factory Heirarchy (2).xlsx" \
        --company JIVO_OIL --commit --backup /tmp/before.json --report /tmp/report.txt

Two workbooks describe one factory and neither is complete.

``Factory New Heirarchy.xlsx`` is a flat person-per-row sheet and it carries the
**JWPL codes**, which are the join key to the punching machines. What it does
not carry is the supervisor layer: it names Management, HOD and staff, three
tiers, so importing it flattens every team onto its HOD.

``Factory Heirarchy (2).xlsx`` is the chart-shaped sheet -- the same shape
:mod:`import_factory_hierarchy` reads -- and it has the **L1 column**, plus the
finance columns (``Category``, ``Budget``, ``Sub Budget``) that exist nowhere
else. What it has no column for at all is the employee code.

So this command does not import anybody's identity. It reads the chart sheet and
writes onto people who are **already in the directory**, filling four things the
JWPL import could not:

* ``reporting_manager`` -- the L1 layer, restoring ``staff -> L1 -> HOD``
* ``category``, ``budget``, ``sub_budget`` -- the finance columns

The employee code, the name, the department and the designation are **never
touched**. Those came from the sheet that is authoritative for them, and this
one is older: it would overwrite a real JWPL code with nothing.

**Names are matched exactly, and near-misses are reported rather than guessed.**
The two sheets disagree on spellings -- ``Pravin Khatoon`` against ``Pravin
Khatun``, ``Bharampal`` against ``Barhampal``, ``Mumtaj Ahmad`` against ``Mumtaj
Ahmed``. Each of those is *probably* one person, and probably is not good enough
here: the directory is now the thing attendance is keyed on, so attaching the
wrong Ram Lal to a supervisor puts a stranger's punches under his name. The
report lists every unmatched name beside its closest candidate, and
``--alias "Sheet Name=Directory Name"`` applies the ones you confirm.

Everything is a dry run without ``--commit``.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from employee_hierarchy import hierarchy
from employee_hierarchy.constants import AuditAction
from employee_hierarchy.models import Department, Employee, EmployeeAuditLog

from .import_factory_hierarchy import (
    Command as ChartCommand,
    NOT_PEOPLE,
    clean,
    key_of,
)

#: Honorific spellings that mean the same man, folded before matching. The same
#: rule the JWPL importer uses -- ``Arvinder Veer Ji`` and ``Arvinder Veerji``.
HONORIFIC = re.compile(r"\bveer\s*ji\b", re.IGNORECASE)

#: How alike two names must be before the report bothers suggesting one for the
#: other. Tuned so ``Khatoon``/``Khatun`` surfaces and two different Singhs do
#: not; it only ever produces a suggestion for a human to read.
SUGGEST_RATIO = 0.82

#: The finance columns this command owns, and nothing else.
FINANCE_FIELDS = ("category", "budget", "sub_budget")

#: Prefix for somebody created from this sheet. It has no code column at all, so
#: the code has to say so rather than look like a JWPL number: these people
#: cannot match a punch until HR enrols them. Same convention as the JWPL
#: importer's own ``NOCODE-nnnn``, with a ``C`` so the source is visible.
CHART_CODE_PREFIX = "NOCODE-C"


def split_name(full_name):
    """``"Vishal Tyagi"`` -> ``("Vishal", "Tyagi")``; a single word has no surname."""
    parts = clean(full_name).split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def match_key(name):
    """Identity for lining the two sheets up: honorifics folded, case ignored."""
    return HONORIFIC.sub("veerji", clean(name)).casefold()


def most_common(counter):
    """The value the sheet says most often, or ``""``."""
    return counter.most_common(1)[0][0] if counter else ""


class Command(BaseCommand):
    help = "Fill the L1 supervisor layer and the finance columns from the chart workbook."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx workbook.")
        parser.add_argument("--company", default="JIVO_OIL", help="Company code to work in.")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without it the command reports what it would do and stops.",
        )
        parser.add_argument("--backup", help="Write the fields being changed to this JSON file.")
        parser.add_argument("--report", help="Write the full decision report to this file.")
        parser.add_argument(
            "--alias",
            action="append",
            default=[],
            metavar="SHEET NAME=DIRECTORY NAME",
            help=(
                "Confirm that a name on the sheet is somebody in the directory, e.g. "
                '--alias "Pravin Khatoon=Pravin Khatun". Repeatable. Only ever needed '
                "for the names the report lists as unmatched."
            ),
        )
        parser.add_argument(
            "--create-missing",
            action="store_true",
            help=(
                "Create the people on the sheet who are not in the directory. They get "
                "no employee code, so they can never match a punch until HR gives them "
                "one -- which is why this is off by default."
            ),
        )
        parser.add_argument(
            "--no-supervisors",
            action="store_true",
            help="Fill only the finance columns; leave the reporting tree alone.",
        )

    # -- reading ---------------------------------------------------------

    def read_plan(self, path):
        """Parse the chart sheet with the importer that already understands it.

        Borrowed rather than reimplemented: that command already knows this
        sheet's tiers, that ``Vacant`` is not a person, that ``Gagan/Prince`` is
        two of them, and how to settle a person named under two different
        superiors by majority vote. A second parser would drift from it.
        """
        chart = ChartCommand()
        rows = chart.read_rows(path)
        people, notes, skipped = chart.build_plan(rows)
        return people, notes, skipped

    # -- matching --------------------------------------------------------

    def directory(self, company):
        """``{match_key: [employees]}`` for everybody already on the roll."""
        found = defaultdict(list)
        for employee in Employee.objects.filter(company=company):
            found[match_key(employee.full_name)].append(employee)
        return found

    def resolve_aliases(self, raw, directory, notes):
        """``--alias`` pairs, checked against the directory before they count."""
        aliases = {}
        # Anybody an earlier run of this command created. Read off the
        # directory, which is already scoped to this company, rather than with
        # a second query.
        stale = {
            name: candidates[0]
            for name, candidates in directory.items()
            if len(candidates) == 1
            and candidates[0].employee_code.startswith(CHART_CODE_PREFIX)
        }
        for item in raw:
            sheet_name, _, directory_name = item.partition("=")
            if not directory_name.strip():
                raise SystemExit(f'--alias wants "SHEET NAME=DIRECTORY NAME", got {item!r}.')
            target = directory.get(match_key(directory_name))
            if not target:
                raise SystemExit(f"--alias names {directory_name!r}, who is not in the directory.")
            if len(target) > 1:
                raise SystemExit(
                    f"--alias names {directory_name!r}, who is on "
                    f"{len(target)} directory rows -- it cannot say which."
                )
            aliases[match_key(sheet_name)] = target[0]
            notes["name confirmed by hand"].append(
                f"{clean(sheet_name)} -> {target[0].full_name} ({target[0].employee_code})"
            )
            # An earlier run, before this alias existed, may have created that
            # very name as a person of its own. The alias now says they are
            # somebody already on the roll, so that record is a leftover -- and
            # a leftover with a team under it is worse than a duplicate, because
            # the team is hanging off a record with no employee code and so no
            # attendance. Never deleted here: it may have history on it by now.
            leftover = stale.get(match_key(sheet_name))
            if leftover is not None and leftover.pk != target[0].pk:
                notes[
                    "LEFTOVER from an earlier run — this alias makes it redundant, delete it"
                ].append(
                    f"{leftover.full_name} ({leftover.employee_code}), "
                    f"{leftover.direct_reports.count()} report(s) — "
                    f"now confirmed to be {target[0].full_name} ({target[0].employee_code})"
                )
        return aliases

    def suggest(self, name, directory):
        """The closest directory name, when there is one worth looking at.

        Two passes, because plain edit distance gets this wrong in a way that
        reads convincingly. ``Gurparvez Singh`` is one edit-ish from
        ``Gurpreet Singh`` and the directory also holds a plain ``Gurparvez`` --
        the right answer -- but the surname makes the wrong one score higher.
        So a name whose words are a subset of a directory name's (or the other
        way round) wins outright, and only then does spelling distance decide.

        Returns ``(employee, why)``. It is only ever a suggestion printed for a
        human: nothing in this command matches on it.
        """
        wanted = set(match_key(name).split())
        # Counted over EVERY directory name first. Dropping the ones held by
        # several people before counting would make an ambiguous case look
        # unique -- there are three Sandeeps, two of them sharing a name, and
        # filtering those out early left "Sandeep Kujur" looking like the only
        # answer when it is one of three.
        subset = [
            other
            for other in directory
            if wanted and (wanted <= set(other.split()) or set(other.split()) <= wanted)
        ]
        if len(subset) == 1 and len(directory[subset[0]]) == 1:
            return directory[subset[0]][0], "same name, one of them with a surname"
        if subset:
            return None, ""  # more than one, so it is not obvious -- say nothing

        best = difflib.get_close_matches(
            match_key(name), list(directory), n=1, cutoff=SUGGEST_RATIO
        )
        if not best:
            return None, ""
        return directory[best[0]][0], "similar spelling — CHECK, this is often a different person"

    # -- the plan --------------------------------------------------------

    def build_changes(self, people, directory, aliases, options, notes):
        """What to write onto whom. Nothing here touches the database."""
        by_name = {}
        for entry in people.values():
            if not entry.name or key_of(entry.name) in NOT_PEOPLE:
                continue
            # A manager and a staff row of the same name are one person as far
            # as *this* sheet is concerned; the richest row wins.
            existing = by_name.get(match_key(entry.name))
            if existing is None or len(entry.rows) > len(existing.rows):
                by_name[match_key(entry.name)] = entry

        changes, unmatched = {}, []
        for key, entry in by_name.items():
            target = aliases.get(key)
            if target is None:
                candidates = directory.get(key, [])
                if len(candidates) > 1:
                    notes["name is on more than one directory row — skipped"].append(
                        f"{entry.name} — employees "
                        f"{', '.join(str(c.id) for c in candidates)}"
                    )
                    continue
                if not candidates:
                    unmatched.append(entry)
                    continue
                target = candidates[0]

            change = {"employee": target, "entry": entry, "fields": {}, "manager": None}

            for field, counter in (
                ("category", entry.categories),
                ("budget", entry.budget),
                ("sub_budget", entry.sub_budget),
            ):
                value = most_common(counter)
                # Only ever fills a gap or corrects a value the sheet actually
                # has an opinion about; a blank cell never blanks the record.
                if value and getattr(target, field) != value:
                    change["fields"][field] = value

            if not options["no_supervisors"]:
                supervisor = most_common(entry.superiors)
                if supervisor and key_of(supervisor) not in NOT_PEOPLE:
                    change["manager"] = supervisor

            changes[target.pk] = change

        for entry in unmatched:
            suggestion, why = self.suggest(entry.name, directory)
            hint = (
                f" — {why}: {suggestion.full_name} ({suggestion.employee_code}); "
                f'confirm with --alias "{entry.name}={suggestion.full_name}"'
                if suggestion
                else " — no candidate"
            )
            notes["on the sheet, not in the directory"].append(f"{entry.name}{hint}")
        return changes, unmatched

    def resolve_managers(self, changes, directory, aliases, notes):
        """Turn each supervisor's *name* into the employee row they are.

        A supervisor who is not in the directory cannot be pointed at, and
        inventing them would put a whole team under a person with no employee
        code and therefore no attendance. Those are reported and the team is
        left where it is.
        """
        placements = {}
        for change in changes.values():
            name = change["manager"]
            if not name:
                continue
            manager = aliases.get(match_key(name))
            if manager is None:
                candidates = directory.get(match_key(name), [])
                if len(candidates) != 1:
                    notes[
                        "supervisor not in the directory — team left where it is"
                        if not candidates
                        else "supervisor's name is on more than one row — team left where it is"
                    ].append(f"{name} (named above {change['employee'].full_name})")
                    continue
                manager = candidates[0]
            if manager.pk == change["employee"].pk:
                continue  # nobody reports to themselves
            if change["employee"].reporting_manager_id != manager.pk:
                placements[change["employee"].pk] = manager
        return placements

    # -- creating --------------------------------------------------------

    def create_missing(self, company, unmatched, notes):
        """Add the people this sheet names who are not on the roll yet.

        Deliberately behind a flag. They arrive with **no employee code**, so
        they appear in the directory and in the org chart but can never match a
        punch -- the code is what the punching machines key on, and this sheet
        has no column for it. The code says so on its face rather than looking
        like a JWPL number that simply never matches.
        """
        taken = set(
            Employee.objects.filter(company=company, employee_code__startswith=CHART_CODE_PREFIX)
            .values_list("employee_code", flat=True)
        )
        departments = {
            dept.name.casefold(): dept for dept in Department.objects.filter(company=company)
        }

        created = []
        counter = 0
        for entry in unmatched:
            counter += 1
            code = f"{CHART_CODE_PREFIX}{counter:03d}"
            while code in taken:
                counter += 1
                code = f"{CHART_CODE_PREFIX}{counter:03d}"
            taken.add(code)

            first, last = split_name(entry.name)
            department = departments.get(most_common(entry.departments).casefold())
            if department is None and most_common(entry.departments):
                notes["created, but their department is not in the directory"].append(
                    f"{entry.name} — sheet says {most_common(entry.departments)!r}"
                )
            # Saved one at a time, not bulk_create: ``full_name`` is derived in
            # Employee.save(), which bulk_create never calls, and it is the key
            # the second planning pass matches these very people on. Created in
            # bulk they came back nameless and could never be attached to
            # anybody. Thirty-odd rows -- the round trips are not the cost here.
            created.append(
                Employee.objects.create(
                    company=company,
                    employee_code=code,
                    first_name=first,
                    last_name=last,
                    department=department,
                    category=most_common(entry.categories),
                    budget=most_common(entry.budget),
                    sub_budget=most_common(entry.sub_budget),
                )
            )
            notes["created from this sheet — no employee code, cannot match a punch"].append(
                f"{entry.name} ({code})"
            )
        return created

    # -- writing ---------------------------------------------------------

    def write(self, changes, placements, backup_path):
        if backup_path:
            payload = [
                {
                    "employee_id": c["employee"].pk,
                    "full_name": c["employee"].full_name,
                    "employee_code": c["employee"].employee_code,
                    "reporting_manager_id": c["employee"].reporting_manager_id,
                    **{f: getattr(c["employee"], f) for f in FINANCE_FIELDS},
                }
                for c in changes.values()
            ]
            with open(backup_path, "w") as handle:
                json.dump(payload, handle, indent=1, default=str)
            self.stdout.write(f"  backup written to {backup_path}")

        audit = []
        touched = 0
        for change in changes.values():
            employee = change["employee"]
            if not change["fields"]:
                continue
            for field, value in change["fields"].items():
                audit.append(
                    EmployeeAuditLog(
                        employee=employee,
                        action=AuditAction.EMPLOYEE_UPDATED,
                        field=field,
                        previous_value=getattr(employee, field),
                        new_value=value,
                        reason="Chart hierarchy workbook — gap fill",
                    )
                )
                setattr(employee, field, value)
            employee.save(update_fields=[*change["fields"], "updated_at"])
            touched += 1

        moved = 0
        # Supervisors first. A supervisor is usually in this same set -- an L1
        # moving under their HOD -- and placing the team first would rewrite
        # their paths once, then again when the supervisor moves. Ordering by
        # how many people report *to* them puts managers ahead of their staff.
        report_counts = Counter(manager.pk for manager in placements.values())
        ordered = sorted(
            placements.items(), key=lambda item: -report_counts.get(item[0], 0)
        )
        for employee_id, manager in ordered:
            employee = Employee.objects.get(pk=employee_id)
            # Through the service, so the cycle check, the depth limit and the
            # subtree path rewrite are the same ones every other move in this
            # module goes through. ``move_to_manager`` carries their team.
            previous = employee.reporting_manager
            try:
                hierarchy.move_to_manager(employee, Employee.objects.get(pk=manager.pk))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                self.stdout.write(
                    self.style.WARNING(
                        f"  could not place {employee.full_name} under "
                        f"{manager.full_name}: {exc}"
                    )
                )
                continue
            audit.append(
                EmployeeAuditLog(
                    employee=employee,
                    action=AuditAction.MANAGER_CHANGED,
                    field="reporting_manager",
                    previous_value=previous.full_name if previous else "",
                    new_value=manager.full_name,
                    reason="Chart hierarchy workbook — L1 supervisor layer",
                )
            )
            moved += 1

        EmployeeAuditLog.objects.bulk_create(audit)
        # One sweep at the end: the moves above are individually correct, but a
        # tree rebuilt from two sheets is worth re-deriving in full rather than
        # trusting that every incremental rewrite composed.
        if moved:
            hierarchy.rebuild_paths()
        return touched, moved

    # -- reporting -------------------------------------------------------

    def render(self, changes, placements, unmatched, notes, committed):
        field_counts = Counter(
            field for c in changes.values() for field in c["fields"]
        )
        lines = [f"Hierarchy gap fill — {'written' if committed else 'plan'}"]
        lines.append(f"  people matched in the directory : {len(changes)}")
        lines.append(f"  on the sheet, no match          : {len(unmatched)}")
        if unmatched and not committed:
            lines.append(
                "      (with --create-missing these are added and the plan is then "
                "re-run, so the supervisor count below will grow)"
            )
        lines.append(f"  supervisors to attach           : {len(placements)}")
        for field in FINANCE_FIELDS:
            lines.append(f"  {field:31s}: {field_counts.get(field, 0)}")
        lines.append("")
        if placements:
            lines.append("Supervisor layer being restored")
            for employee_id, manager in list(placements.items())[:40]:
                employee = Employee.objects.filter(pk=employee_id).first()
                if employee:
                    lines.append(f"    - {employee.full_name} -> {manager.full_name}")
            if len(placements) > 40:
                lines.append(f"    … and {len(placements) - 40} more")
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

    # -- entry point -----------------------------------------------------

    def handle(self, *args, **options):
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise SystemExit(f"No company with code {options['company']}.")

        notes = defaultdict(list)
        people, chart_notes, _skipped = self.read_plan(options["file"])
        for heading, entries in chart_notes.items():
            notes[f"from the sheet: {heading}"].extend(entries)

        directory = self.directory(company)
        aliases = self.resolve_aliases(options["alias"], directory, notes)
        changes, unmatched = self.build_changes(people, directory, aliases, options, notes)
        placements = self.resolve_managers(changes, directory, aliases, notes)

        if not options["commit"]:
            report = self.render(changes, placements, unmatched, notes, False)
            self.stdout.write(report)
            if options["report"]:
                with open(options["report"], "w") as handle:
                    handle.write(report)
            self.stdout.write(
                self.style.WARNING("\nDry run — nothing written. Pass --commit to apply.")
            )
            return

        with transaction.atomic():
            created = 0
            if options["create_missing"] and unmatched:
                new_people = self.create_missing(company, unmatched, notes)
                created = len(new_people)
                # Plan again now they exist. Two things only become possible
                # once they are on the roll: they can be given their own
                # supervisor, and a supervisor who was missing a moment ago can
                # now be pointed at by their whole team.
                carried = {
                    heading: entries
                    for heading, entries in notes.items()
                    if "created" in heading or "confirmed by hand" in heading
                }
                notes = defaultdict(list, carried)
                directory = self.directory(company)
                changes, unmatched = self.build_changes(
                    people, directory, aliases, options, notes
                )
                placements = self.resolve_managers(changes, directory, aliases, notes)

            touched, moved = self.write(changes, placements, options["backup"])

        report = self.render(changes, placements, unmatched, notes, True)
        self.stdout.write(report)
        if options["report"]:
            with open(options["report"], "w") as handle:
                handle.write(report)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nWritten: {touched} people updated, {moved} placed under a supervisor"
                + (f", {created} created." if created else ".")
            )
        )
