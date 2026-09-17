"""A group for whoever clears a truck to come in for dispatch after the cutoff.

Mints the group only -- who ends up in it is an administrator's decision, not a
migration's. Mirrors ``docking_admin.0002_create_docking_admin_groups``: the gate
needs no right to *ask* (anyone who can start an empty-vehicle gate-in can raise
the request), so there is no requester group here, only the approver's.
"""

from django.db import migrations

GROUP_NAME = "Late Dispatch Gate-In Approver"
CODENAMES = [
    "can_view_late_dispatch_gate_in",
    "can_approve_late_dispatch_gate_in",
]


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct = ContentType.objects.filter(
        app_label="gate_core", model="latedispatchgateinapproval"
    ).first()
    if ct is None:
        # Permissions are created by the post-migrate hook, which has not run yet on
        # a from-scratch migrate. The group is seeded on the next run.
        return

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set(
        Permission.objects.filter(content_type=ct, codename__in=CODENAMES)
    )


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0060_latedispatchgateinapproval"),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
