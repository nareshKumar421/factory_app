"""
Create the standard leave types for a company.

Unlike ``seed_leave_demo``, this is **not** throwaway data -- it is the master
list a plant actually runs on, and it is safe to run against a live database.
A company with no leave types cannot accept a single application, so this is
the first thing to run after ``migrate`` on a new deployment.

    python manage.py seed_leave_types --company JIVO_OIL            # dry run
    python manage.py seed_leave_types --company JIVO_OIL --commit
    python manage.py seed_leave_types --all-companies --commit

**Existing types are never touched.** Matching is on ``code`` per company, and
a code that already exists is reported and left exactly as it is -- quotas get
tuned per plant after the first year, and an import that "corrected" them back
to these defaults would silently rewrite somebody's entitlement. Use the
Leave settings screen, or ``PATCH /api/v1/leave/types/{id}/``, to change one.

``--reorder`` is the one exception, and only because ``sort_order`` is not
policy: it decides where a type sits in the apply dropdown and nothing else. A
company seeded in two passes ends up with collisions (unpaid leave landing in
the middle of the list rather than last), and fixing that cannot cost anybody a
day of entitlement. It rewrites ``sort_order`` alone and leaves every other
column, including retired types, exactly as it found them.

**The figures below are defaults, not law.** Two are statutory and the rest are
common practice; all of them are now *enforced* at the point of application, so
a quota that is wrong for this plant will refuse real requests. Check them
against the HR policy before running with ``--commit``:

* **Maternity 182 days** is the Maternity Benefit (Amendment) Act 2017 -- 26
  weeks. This one is a legal floor, not a preference.
* **Earned leave 18 days** reflects the Factories Act 1948 rule of one day per
  20 days worked; plants that count it differently should adjust.
* Casual, sick, paternity, bereavement and marriage leave are **not** statutory
  for this kind of establishment. The numbers here are ordinary Indian
  manufacturing practice and are the ones most likely to need changing.

**Compensatory off carries no quota on purpose.** It is *earned* by working a
holiday or a weekly off, one day at a time -- it is not an annual allotment, so
a ceiling would be meaningless. Quota 0 means untracked, which is exactly
right: the balance screen shows what has been taken and imposes no limit.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company

from ...constants import RecordStatus
from ...models import LeaveType

#: (code, name, is_paid, allow_half_day, requires_document, annual_quota,
#:  max_consecutive_days, description)
#:
#: Ordered as HR reads them: the everyday three first, then the earned one,
#: then the life-event ones, with unpaid leave last because it is the fallback
#: when everything else is exhausted.
STANDARD_TYPES = [
    (
        "CL", "Casual Leave", True, True, False, 12, 0,
        "Short notice, personal reasons. Usually a day or two at a time.",
    ),
    (
        "SL", "Sick Leave", True, False, True, 10, 0,
        "Illness. A medical certificate is expected.",
    ),
    (
        "EL", "Earned Leave", True, True, False, 18, 0,
        "Accrued against days worked. The one people save up and plan around.",
    ),
    (
        "CO", "Compensatory Off", True, True, False, 0, 0,
        "Earned by working a holiday or a weekly off. Not an annual allotment, "
        "so it carries no quota.",
    ),
    (
        "ML", "Maternity Leave", True, False, True, 182, 182,
        "26 weeks under the Maternity Benefit (Amendment) Act 2017. Statutory.",
    ),
    (
        "PL", "Paternity Leave", True, False, False, 15, 15,
        "Around the birth of a child. Not statutory for private establishments.",
    ),
    (
        "BL", "Bereavement Leave", True, False, False, 3, 3,
        "A death in the immediate family. No paperwork is asked for.",
    ),
    (
        "MRL", "Marriage Leave", True, False, False, 5, 5,
        "The employee's own marriage.",
    ),
    (
        "LWP", "Leave Without Pay", False, False, False, 0, 0,
        "Unpaid. No ceiling worth storing, so it is untracked.",
    ),
]


class Command(BaseCommand):
    help = "Create the standard leave types for a company (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code, e.g. JIVO_OIL.")
        parser.add_argument(
            "--all-companies",
            action="store_true",
            help="Every active company. A plant with no types cannot accept leave at all.",
        )
        parser.add_argument(
            "--reorder",
            action="store_true",
            help="Also renumber sort_order on existing types (presentation only).",
        )
        parser.add_argument(
            "--commit", action="store_true", help="Actually write. Dry run without it."
        )

    def handle(self, *args, **options):
        if options["all_companies"]:
            companies = list(Company.objects.filter(is_active=True).order_by("code"))
            if not companies:
                raise CommandError("No active companies.")
        elif options.get("company"):
            company = Company.objects.filter(code=options["company"]).first()
            if company is None:
                raise CommandError(f"No company with code {options['company']!r}.")
            companies = [company]
        else:
            raise CommandError("Pass --company CODE or --all-companies.")

        total_created = 0
        total_existing = 0

        for company in companies:
            created, existing = self._seed(
                company, commit=options["commit"], reorder=options["reorder"]
            )
            total_created += created
            total_existing += existing

        self.stdout.write("")
        if options["commit"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{total_created} type(s) created, {total_existing} left as they were."
                )
            )
            if total_created:
                self.stdout.write(
                    "Quotas are enforced when somebody applies. Check them against "
                    "the HR policy on the Leave settings screen."
                )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run -- {total_created} type(s) would be created, "
                    f"{total_existing} already exist. Re-run with --commit."
                )
            )

    def _seed(self, company, *, commit, reorder=False):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"{company.code}"))

        existing = {
            code.upper(): name
            for code, name in LeaveType.objects.filter(company=company).values_list(
                "code", "name"
            )
        }

        created = 0
        skipped = 0
        to_create = []

        for order, row in enumerate(STANDARD_TYPES):
            (
                code,
                name,
                is_paid,
                allow_half_day,
                requires_document,
                quota,
                max_run,
                description,
            ) = row

            if code.upper() in existing:
                self.stdout.write(
                    f"    = {code:4} {name:20} already exists as "
                    f"{existing[code.upper()]!r} -- left alone"
                )
                skipped += 1
                continue

            to_create.append(
                LeaveType(
                    company=company,
                    code=code,
                    name=name,
                    description=description,
                    is_paid=is_paid,
                    allow_half_day=allow_half_day,
                    requires_document=requires_document,
                    annual_quota=quota,
                    max_consecutive_days=max_run,
                    status=RecordStatus.ACTIVE,
                    sort_order=order,
                )
            )
            quota_text = f"{quota}/yr" if quota else "untracked"
            flags = ", ".join(
                filter(
                    None,
                    [
                        "" if is_paid else "unpaid",
                        "half days" if allow_half_day else "",
                        "needs paperwork" if requires_document else "",
                        f"max {max_run} in a row" if max_run else "",
                    ],
                )
            )
            self.stdout.write(
                f"    + {code:4} {name:20} {quota_text:12}"
                + (f" ({flags})" if flags else "")
            )
            created += 1

        if commit and to_create:
            with transaction.atomic():
                LeaveType.objects.bulk_create(to_create)

        if reorder:
            self._reorder(company, commit=commit)

        return created, skipped

    def _reorder(self, company, *, commit):
        """Renumber sort_order to the canonical order. Touches nothing else."""
        canonical = {row[0].upper(): order for order, row in enumerate(STANDARD_TYPES)}
        # Anything not in the standard list keeps its relative place, after the
        # ones that are -- a company's own extra type should not be shuffled to
        # the top by a renumbering it had no part in.
        tail = len(STANDARD_TYPES)

        moved = []
        for leave_type in LeaveType.objects.filter(company=company):
            wanted = canonical.get(leave_type.code.upper())
            if wanted is None:
                wanted = tail + leave_type.pk
            if leave_type.sort_order != wanted:
                moved.append((leave_type, wanted))

        if not moved:
            return

        for leave_type, wanted in moved:
            self.stdout.write(
                f"    ~ {leave_type.code:4} sort_order {leave_type.sort_order} -> {wanted}"
            )
            leave_type.sort_order = wanted

        if commit:
            with transaction.atomic():
                LeaveType.objects.bulk_update(
                    [lt for lt, _ in moved], ["sort_order", "updated_at"]
                )
