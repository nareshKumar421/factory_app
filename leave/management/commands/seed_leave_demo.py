"""
Fill a local database with leave data worth looking at.

Not fixtures for the test suite -- those build exactly what they assert on.
This is for the screens: a queue with something in it, a calendar with enough
overlap to show a staffing problem, and every status represented so nobody has
to manufacture a rejected request by hand to see what one looks like.

**It refuses to run against production.** The guard is ``DEBUG``: the default
settings module reads ``.env``, which points at the live database, and ``DEBUG``
is false there. ``config.local_dev_settings`` sets it true. So::

    python manage.py seed_leave_demo --commit --settings=config.local_dev_settings

and the same command against the default settings stops before it writes
anything. ``--force`` overrides the guard and exists only for a deployment
whose DEBUG is true for some other reason; it is not a way to seed production.

What it makes, for one company:

* the 2026 holiday calendar (10 mandatory, 3 restricted)
* a leave request for every person it can reach, spread across PENDING,
  APPROVED, REJECTED, WITHDRAWN and CANCELLED
* half days, single days and multi-day spans, including ones that straddle a
  Sunday so the "weekly offs are not charged" rule is visible on screen
* a cluster of requests on the same dates, so the calendar's per-day headcount
  has something to report

Everything is routed and decided through :mod:`leave.services` and
:mod:`leave.routing` -- the same path the API uses. Nothing is written straight
to the tables, so the trail, the day rows and the attendance projection all
come out exactly as they would in real use. Requests that the rules refuse
(a clashing date, somebody who has left) are skipped and counted, not forced.

Re-running adds nothing: seeded requests are recognised by a marker in their
reason, and ``--clear`` removes them.
"""

import random
from datetime import date, timedelta

from django.core.files.base import ContentFile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from company.models import Company
from employee_hierarchy.constants import IN_SERVICE_STATUSES
from employee_hierarchy.models import Employee

from ...constants import DayPortion, LeaveRequestStatus, RecordStatus
from ...models import Holiday, LeaveRequest, LeaveType
from ...projection import project_request, unproject_request
from ...routing import responsible_manager
from ...services import LeaveRefused, apply_for_leave, approve, cancel, reject, withdraw

#: Stamped into every seeded reason so the data can be found and removed again.
MARKER = "[demo]"

#: Indian factory holidays for 2026. Dates are illustrative -- a real calendar
#: is maintained by HR, and this is here so the "holidays are not charged" rule
#: has something to bite on.
HOLIDAYS_2026 = [
    (date(2026, 1, 1), "New Year's Day", False),
    (date(2026, 1, 26), "Republic Day", False),
    (date(2026, 3, 4), "Holi", False),
    (date(2026, 3, 21), "Id-ul-Fitr", True),
    (date(2026, 4, 14), "Ambedkar Jayanti", False),
    (date(2026, 5, 1), "Labour Day", False),
    (date(2026, 8, 15), "Independence Day", False),
    (date(2026, 10, 2), "Gandhi Jayanti", False),
    (date(2026, 10, 20), "Dussehra", False),
    (date(2026, 11, 8), "Diwali", False),
    (date(2026, 11, 24), "Guru Nanak Jayanti", True),
    (date(2026, 12, 25), "Christmas", False),
    (date(2026, 12, 31), "Year end", True),
]

REASONS = [
    "Family function at home",
    "Medical appointment",
    "Attending a wedding out of town",
    "Personal work at the bank",
    "Child's school event",
    "Travelling to the village",
    "Not keeping well",
    "House shifting",
    "Court appearance",
    "Festival at home",
]

DECISION_NOTES = {
    "APPROVED": ["Approved.", "Fine, arrange cover.", "Approved — inform your line."],
    "REJECTED": [
        "Peak dispatch week, cannot spare you.",
        "Two people already off those days.",
        "Apply again after the audit.",
    ],
    "CANCELLED": ["Came in after all.", "Plan changed, employee working."],
}


class Command(BaseCommand):
    help = "Seed demo leave data on a LOCAL database (refuses unless DEBUG is on)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_OIL", help="Company code.")
        parser.add_argument(
            "--employees",
            type=int,
            default=40,
            help="How many people to generate leave for. Default 40.",
        )
        parser.add_argument(
            "--seed", type=int, default=20260922, help="RNG seed, so runs repeat."
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove previously seeded demo data and stop.",
        )
        parser.add_argument(
            "--commit", action="store_true", help="Actually write. Dry run without it."
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip the DEBUG guard. Not a way to seed production.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "DEBUG is off, which means this is probably the live database. "
                "Run with --settings=config.local_dev_settings, or pass --force "
                "if you are certain."
            )

        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {options['company']!r}.")

        if options["clear"]:
            self._clear(company, commit=options["commit"])
            return

        random.seed(options["seed"])

        types = list(
            LeaveType.objects.filter(company=company, status=RecordStatus.ACTIVE).order_by(
                "sort_order"
            )
        )
        if not types:
            raise CommandError(
                "No active leave types for this company. Create some first "
                "(POST /api/v1/leave/types/, or the Django admin)."
            )

        if not options["commit"]:
            self.stdout.write(
                self.style.WARNING("Dry run — nothing will be written. Add --commit.")
            )
            self._preview(company, types, options["employees"])
            return

        with transaction.atomic():
            holidays = self._seed_holidays(company)
        self.stdout.write(f"  holidays          : {holidays} added")

        made = self._seed_requests(company, types, options["employees"])
        self._report(made)

    # -- holidays ------------------------------------------------------------

    def _seed_holidays(self, company):
        added = 0
        for day, name, optional in HOLIDAYS_2026:
            _, created = Holiday.objects.get_or_create(
                company=company,
                date=day,
                defaults={"name": name, "is_optional": optional},
            )
            added += 1 if created else 0
        return added

    # -- requests ------------------------------------------------------------

    def _candidates(self, company, limit):
        """People who can hold a request, those with a signed-in approver first.

        The ordering is the point. A seeded request whose approver has no login
        is decided by nobody and shows up in nobody's queue -- which makes the
        approvals screen look broken when it is merely unreachable. So people
        whose responsible manager *can* sign in are taken first, and the rest
        only fill the remainder.
        """
        employees = list(
            Employee.objects.filter(
                company=company, employment_status__in=IN_SERVICE_STATUSES
            )
            .exclude(reporting_manager__isnull=True)
            .select_related("reporting_manager", "department")
        )
        random.shuffle(employees)

        reachable, unreachable = [], []
        for employee in employees:
            approver = responsible_manager(employee)
            target = reachable if (approver and approver.user_id) else unreachable
            target.append(employee)
            if len(reachable) >= limit:
                break

        return (reachable + unreachable)[:limit]

    def _seed_requests(self, company, types, limit):
        today = timezone.localdate()
        employees = self._candidates(company, limit)
        made = {
            "PENDING": 0,
            "APPROVED": 0,
            "REJECTED": 0,
            "WITHDRAWN": 0,
            "CANCELLED": 0,
            "skipped": 0,
            "projected": 0,
            "no_approver": 0,
        }

        # A cluster of dates everybody draws from, so the calendar shows real
        # overlap rather than one person per column.
        hot_dates = [today + timedelta(days=offset) for offset in (3, 4, 5, 10, 11)]

        for index, employee in enumerate(employees):
            leave_type = random.choice(types)
            shape = index % 5

            if shape == 0:  # a past single day — lands on the attendance sheet
                start = today - timedelta(days=random.randint(1, 10))
                end = start
                portion = DayPortion.FULL
            elif shape == 1:  # a half day
                start = today - timedelta(days=random.randint(1, 6))
                end = start
                portion = (
                    DayPortion.FIRST_HALF if leave_type.allow_half_day else DayPortion.FULL
                )
            elif shape == 2:  # a span that straddles a Sunday
                start = today + timedelta(days=random.randint(2, 6))
                end = start + timedelta(days=4)
                portion = DayPortion.FULL
            elif shape == 3:  # one of the clustered dates
                start = random.choice(hot_dates)
                end = start
                portion = DayPortion.FULL
            else:  # a two-day future request
                start = today + timedelta(days=random.randint(7, 20))
                end = start + timedelta(days=1)
                portion = DayPortion.FULL

            approver = responsible_manager(employee)
            decider = approver.user if approver is not None else None
            if decider is None:
                made["no_approver"] += 1

            # A type that expects paperwork gets a placeholder, otherwise every
            # sick-leave request would be refused and the demo would quietly
            # contain only the types that need nothing attached.
            document = (
                ContentFile(
                    b"%PDF-1.4 placeholder medical certificate (demo data)",
                    name=f"demo-certificate-{employee.employee_code}.pdf",
                )
                if leave_type.requires_document
                else None
            )

            try:
                with transaction.atomic():
                    request = apply_for_leave(
                        employee=employee,
                        leave_type=leave_type,
                        from_date=start,
                        to_date=end,
                        portion=portion,
                        reason=f"{random.choice(REASONS)} {MARKER}",
                        contact_number=f"9{random.randint(100000000, 999999999)}",
                        document=document,
                        applied_by=employee.user,
                        # Demo data is about filling screens, not about
                        # exercising the quota -- a year's worth of realistic
                        # requests would otherwise be refused halfway through
                        # and the spread of statuses would collapse. The quota
                        # has its own tests.
                        allow_overdraw=True,
                    )
            except LeaveRefused:
                # A clash with something already seeded, or a span that is all
                # weekly offs. Expected; the rules are doing their job.
                made["skipped"] += 1
                continue

            outcome = self._outcome(index)
            try:
                made[self._decide(request, outcome, decider)] += 1
            except LeaveRefused:
                made["skipped"] += 1
                continue

            if request.status == LeaveRequestStatus.APPROVED:
                made["projected"] += project_request(request, user=decider)

        return made

    def _outcome(self, index):
        """A spread of statuses, weighted towards what a real queue looks like."""
        return [
            "PENDING",
            "APPROVED",
            "PENDING",
            "APPROVED",
            "REJECTED",
            "APPROVED",
            "PENDING",
            "WITHDRAWN",
            "APPROVED",
            "CANCELLED",
        ][index % 10]

    def _decide(self, request, outcome, decider):
        if outcome == "PENDING":
            return "PENDING"

        if outcome == "WITHDRAWN":
            withdraw(request, user=request.applied_by, comment=f"Changed plans {MARKER}")
            return "WITHDRAWN"

        if outcome == "REJECTED":
            reject(
                request,
                user=decider,
                comment=random.choice(DECISION_NOTES["REJECTED"]),
                authority="manager",
            )
            return "REJECTED"

        approve(
            request,
            user=decider,
            comment=random.choice(DECISION_NOTES["APPROVED"]),
            authority="manager",
        )
        if outcome == "CANCELLED":
            project_request(request, user=decider)
            cancel(
                request,
                user=decider,
                comment=random.choice(DECISION_NOTES["CANCELLED"]),
                authority="manager",
            )
            unproject_request(request, user=decider)
            return "CANCELLED"
        return "APPROVED"

    # -- reporting and clearing ----------------------------------------------

    def _preview(self, company, types, limit):
        employees = self._candidates(company, limit)
        with_approver = sum(
            1 for e in employees if (responsible_manager(e) or None) is not None
        )
        self.stdout.write("")
        self.stdout.write(f"  company           : {company.code}")
        self.stdout.write(f"  active leave types: {len(types)}")
        self.stdout.write(f"  people it would use: {len(employees)}")
        self.stdout.write(f"    of those, with a reachable approver: {with_approver}")
        self.stdout.write(f"  holidays it would add: up to {len(HOLIDAYS_2026)}")

    def _report(self, made):
        self.stdout.write("")
        for label in ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "CANCELLED"):
            self.stdout.write(f"  {label.lower():18}: {made[label]}")
        self.stdout.write(f"  {'projected days':18}: {made['projected']}")
        self.stdout.write(
            f"  {'skipped':18}: {made['skipped']} (clashing dates or all-weekly-off spans)"
        )
        if made["no_approver"]:
            self.stdout.write(
                f"  {'no approver login':18}: {made['no_approver']} "
                "(decided anonymously — link more logins to fix)"
            )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Seeded. Re-run with --clear to remove."))

    def _clear(self, company, *, commit):
        seeded = LeaveRequest.objects.filter(company=company, reason__contains=MARKER)
        count = seeded.count()

        if not commit:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — {count} seeded request(s) would be removed. Add --commit."
                )
            )
            return

        with transaction.atomic():
            # Take the approved ones back off the attendance sheet first, so
            # deleting the rows does not strand an override nobody can explain.
            reverted = 0
            for request in seeded.filter(status=LeaveRequestStatus.APPROVED):
                reverted += unproject_request(request)[0]
            seeded.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {count} seeded request(s); {reverted} attendance day(s) reverted."
            )
        )
