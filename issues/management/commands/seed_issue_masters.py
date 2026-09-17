"""
Seed the issue tracker's labels.

Usage::

    python manage.py seed_issue_masters          # create anything missing
    python manage.py seed_issue_masters --list   # show what would be seeded

The set is GitHub's own nine defaults, unchanged.

Idempotent: a row that already exists is left exactly as it is, so a team that
recolours "bug" does not get overwritten on the next deploy. Only genuinely
absent rows are created -- which is also why these are master rows and not a
choices list: a team adds its own labels ("sap", "print", "data issue") from
the tracker's settings screen, and they survive every later deploy.

Running this by hand is optional: migration ``0004_seed_github_labels`` puts
the same nine rows in at deploy time. The command stays for re-seeding a label
somebody deleted, and for ``--list``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from issues.models import IssueLabel

#: GitHub's own default label set, verbatim: the nine labels a new repository
#: is created with, with their exact names, hex colours and descriptions, in
#: GitHub's alphabetical order. Kept identical on purpose -- the tracker is
#: shaped like a GitHub issue list, and anyone who has used one recognises
#: "bug" in that red and "enhancement" in that pale blue without reading them.
#:
#: (name, colour, description, order)
LABELS = [
    ("bug", "#d73a4a", "Something isn't working", 10),
    ("documentation", "#0075ca", "Improvements or additions to documentation", 20),
    ("duplicate", "#cfd3d7", "This issue or pull request already exists", 30),
    ("enhancement", "#a2eeef", "New feature or request", 40),
    ("good first issue", "#7057ff", "Good for newcomers", 50),
    ("help wanted", "#008672", "Extra attention is needed", 60),
    ("invalid", "#e4e669", "This doesn't seem right", 70),
    ("question", "#d876e3", "Further information is requested", 80),
    ("wontfix", "#ffffff", "This will not be worked on", 90),
]

class Command(BaseCommand):
    help = "Create the issue tracker's default labels (idempotent)."

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

        self.stdout.write(
            self.style.SUCCESS(
                f"Labels: {labels_made} created, "
                f"{len(LABELS) - labels_made} already present."
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

    def _show(self):
        existing_labels = set(IssueLabel.objects.values_list("name", flat=True))
        self.stdout.write("Labels:")
        for name, color, _description, _sequence in LABELS:
            mark = "present" if name in existing_labels else "would create"
            self.stdout.write(f"  {name:<14} {color:<8} {mark}")
