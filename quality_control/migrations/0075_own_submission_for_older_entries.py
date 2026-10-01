"""Give every entry made before 0074 a submission of its own.

Entries filled together share a submission and are decided as one; an older
entry was filled alone, so it is a submission of one. Kept apart from 0074's
table change: Postgres refuses data and schema changes in one transaction here.
Reverse does nothing: 0074's reverse drops the column and the table.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Entry = apps.get_model("quality_control", "ProductionQCEntry")
    Submission = apps.get_model("quality_control", "ProductionQCSubmission")

    entries = list(
        Entry.objects.using(db).filter(submission__isnull=True).only("id", "company_id", "created_by_id")
    )
    if not entries:
        return
    submissions = Submission.objects.using(db).bulk_create([
        Submission(company_id=entry.company_id, created_by_id=entry.created_by_id)
        for entry in entries
    ])
    for entry, submission in zip(entries, submissions):
        entry.submission_id = submission.pk
    Entry.objects.using(db).bulk_update(entries, ["submission"])


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0074_report_submissions"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
