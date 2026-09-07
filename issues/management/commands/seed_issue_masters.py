"""
Seed the issue tracker's labels and areas.

Usage::

    python manage.py seed_issue_masters          # create anything missing
    python manage.py seed_issue_masters --list   # show what would be seeded

Idempotent: a row that already exists is left exactly as it is, so a team that
recolours "bug" or renames an area does not get overwritten on the next deploy.
Only genuinely absent rows are created.

The areas mirror the app's own sidebar, because that is how a reporter thinks
about where the problem was ("it happened on the dispatch screen"). Adding a
module to the app means adding a row here -- or, more likely, adding it through
the tracker's own settings screen, which is why these are master rows and not a
choices list.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from issues.models import IssueArea, IssueLabel

#: (name, colour, description, order). Colours follow GitHub's defaults where a
#: label means the same thing, so the list looks familiar at a glance.
LABELS = [
    ("bug", "#d73a4a", "Something is not working", 10),
    ("blocker", "#b60205", "Work has stopped until this is fixed", 20),
    ("data issue", "#e99695", "Wrong or missing data rather than broken code", 30),
    ("sap", "#1d76db", "Involves SAP, HANA or the Service Layer", 40),
    ("enhancement", "#a2eeef", "A new feature or an improvement", 50),
    ("ui", "#c5def5", "Layout, wording or usability", 60),
    ("performance", "#fbca04", "Slow pages, slow reports, timeouts", 70),
    ("permissions", "#5319e7", "Someone can see too much or too little", 80),
    ("print", "#bfd4f2", "Prints, labels, stickers and gate passes", 90),
    ("question", "#d876e3", "Needs more information from the reporter", 100),
    ("duplicate", "#cfd3d7", "Already reported somewhere else", 110),
    ("wont fix", "#ffffff", "Understood, but not going to be changed", 120),
    ("needs triage", "#ededed", "Not yet looked at", 130),
]

#: (name, code, description, order). One per module the app actually has.
AREAS = [
    ("Gate", "gate", "Gate-in, gate-out, weighment, security checks", 10),
    ("Dispatch", "dispatch", "Dispatch plans, docking, bills linking, gate passes", 20),
    ("Warehouse & WMS", "warehouse", "Warehouse ops, pallets, putaway, transfers", 30),
    ("Barcode & Scanning", "barcode", "Box generation, labels and scan screens", 40),
    ("Production", "production", "Production runs, blowing, costing", 50),
    ("Planning & Purchase", "planning-purchase", "Plans, BOM explosion, purchase orders", 60),
    ("Quality Control", "qc", "QC checks, inspection reports, QA procedures", 70),
    ("Maintenance", "maintenance", "Maintenance registers and daily logs", 80),
    ("ETP / STP", "etp", "Treatment plant registers", 90),
    ("Marketplace", "marketplace", "Flipkart / Amazon orders and dispatches", 100),
    ("Returns", "returns", "Goods returns and returnable items", 110),
    ("Finance & Invoicing", "finance", "A/R invoices, approvals, expenses, budgets", 120),
    ("SAP Reports", "sap-reports", "Report list and report pages", 130),
    ("Labour & Attendance", "labour", "Labour count, labour gate, attendance", 140),
    ("Dashboards", "dashboards", "Stock, sales and management dashboards", 150),
    ("Notifications", "notifications", "Push notifications and the bell", 160),
    ("Admin & Access", "admin", "Users, groups, permissions, company switching", 170),
    ("Mobile / PWA", "pwa", "Install, offline behaviour, camera and scanners", 180),
    ("Other", "other", "Anything that does not fit an area above", 900),
]


class Command(BaseCommand):
    help = "Create the issue tracker's default labels and areas (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the defaults and what already exists, without writing.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._show()
            return

        with transaction.atomic():
            labels_made = self._seed_labels()
            areas_made = self._seed_areas()

        self.stdout.write(
            self.style.SUCCESS(
                f"Labels: {labels_made} created, "
                f"{len(LABELS) - labels_made} already present. "
                f"Areas: {areas_made} created, "
                f"{len(AREAS) - areas_made} already present."
            )
        )

    def _seed_labels(self):
        created = 0
        for name, color, description, sequence in LABELS:
            _, made = IssueLabel.objects.get_or_create(
                name=name,
                defaults={
                    "color": color,
                    "description": description,
                    "sequence": sequence,
                },
            )
            created += int(made)
        return created

    def _seed_areas(self):
        created = 0
        for name, code, description, sequence in AREAS:
            _, made = IssueArea.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "sequence": sequence,
                },
            )
            created += int(made)
        return created

    def _show(self):
        existing_labels = set(IssueLabel.objects.values_list("name", flat=True))
        existing_areas = set(IssueArea.objects.values_list("code", flat=True))
        self.stdout.write("Labels:")
        for name, color, _description, _sequence in LABELS:
            mark = "present" if name in existing_labels else "would create"
            self.stdout.write(f"  {name:<14} {color:<8} {mark}")
        self.stdout.write("Areas:")
        for name, code, _description, _sequence in AREAS:
            mark = "present" if code in existing_areas else "would create"
            self.stdout.write(f"  {code:<20} {name:<24} {mark}")
