"""Areas leave the tracker.

Three things go: the ``area`` column on an issue, the ``IssueArea`` master
itself, and the AREA_CHANGED timeline events. That last one is the deliberate
exception to the tracker's append-only timeline -- an event that reads "Area
changed from Dispatch to Gate" describes a field the software no longer has,
and leaving it would make a history that cannot be understood.

Irreversible in substance: reversing the schema brings the table back empty.
"""


from django.db import migrations, models


def drop_area_events(apps, schema_editor):
    IssueEvent = apps.get_model("issues", "IssueEvent")
    IssueEvent.objects.filter(event="AREA_CHANGED").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('issues', '0002_supportcontact'),
    ]

    operations = [
        migrations.RunPython(drop_area_events, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='issue',
            name='area',
        ),
        migrations.AlterModelOptions(
            name='issuepermission',
            options={'default_permissions': (), 'managed': False, 'permissions': [('can_view_issues', 'Can view the issue tracker'), ('can_create_issues', 'Can report a new issue'), ('can_triage_issues', 'Can triage any issue (label, assign, close, reopen, edit)'), ('can_manage_issue_settings', 'Can manage issue labels and settings')], 'verbose_name': 'Issue Tracker', 'verbose_name_plural': 'Issue Tracker'},
        ),
        migrations.AlterField(
            model_name='issueevent',
            name='event',
            field=models.CharField(choices=[('OPENED', 'Opened'), ('CLOSED', 'Closed'), ('REOPENED', 'Reopened'), ('LABELED', 'Labeled'), ('UNLABELED', 'Unlabeled'), ('ASSIGNED', 'Assigned'), ('UNASSIGNED', 'Unassigned'), ('RENAMED', 'Renamed'), ('EDITED', 'Description edited'), ('PRIORITY_CHANGED', 'Priority changed'), ('MARKED_DUPLICATE', 'Marked as duplicate'), ('PINNED', 'Pinned'), ('UNPINNED', 'Unpinned'), ('LOCKED', 'Locked'), ('UNLOCKED', 'Unlocked')], max_length=20),
        ),
        migrations.DeleteModel(
            name='IssueArea',
        ),
    ]
