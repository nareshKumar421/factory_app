"""GitHub's nine default labels land in the database.

The label set is data, not schema, and until now it only existed in
``seed_issue_masters`` -- a command somebody has to remember on every
database. Seeding it here means a migrated database has labels, full stop,
and the tracker's label picker is never empty on the day it goes live.

The list is **copied** into this migration rather than imported from the
command, on purpose: a migration has to keep meaning what it meant when it
ran, and the command's list is free to grow afterwards.

Idempotent and non-destructive. Rows are matched by name and only created
when absent, so a label a team has since recoloured, described differently or
deactivated is left exactly as it is. Nothing is deleted going backwards
either -- the reverse is a no-op, because a label that has been put on issues
is not the migration's to take away.
"""

from django.db import migrations

#: (name, colour, description, order) -- GitHub's defaults, verbatim.
GITHUB_LABELS = [
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


def seed_labels(apps, schema_editor):
    IssueLabel = apps.get_model("issues", "IssueLabel")
    for name, color, description, sequence in GITHUB_LABELS:
        IssueLabel.objects.get_or_create(
            name=name,
            defaults={
                "color": color,
                "description": description,
                "sequence": sequence,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("issues", "0003_remove_issue_areas"),
    ]

    operations = [
        migrations.RunPython(seed_labels, migrations.RunPython.noop),
    ]
