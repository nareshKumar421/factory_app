"""Create the 'labour' permission group.

Assigning this group to a user shows ONLY the Labour module (the /labour
department-allocation screen) in the sidebar, and lets them record the split.
It deliberately excludes every other app's permissions so no other module
appears. Permissions:

  - labour_gate.view_labourgateentry   view the day's labour entries
  - labour_gate.can_record_labour_in   add / edit / delete the department split
  - person_gatein.view_contractor      load the contractor dropdown
  - person_gatein.add_contractor       create a contractor inline (optional)
"""

from django.db import migrations

LABOUR_GROUP = "labour"

LABOUR_GROUP_PERMISSIONS = [
    ("labour_gate", "view_labourgateentry"),
    ("labour_gate", "can_record_labour_in"),
    ("person_gatein", "view_contractor"),
    ("person_gatein", "add_contractor"),
]


def create_labour_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group, _ = Group.objects.get_or_create(name=LABOUR_GROUP)
    perms = []
    for app_label, codename in LABOUR_GROUP_PERMISSIONS:
        perm = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if perm:
            perms.append(perm)
    group.permissions.set(perms)


def remove_labour_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=LABOUR_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("labour_gate", "0004_labourgateaudit"),
        ("person_gatein", "0007_entrylog_actual_entry_time"),
    ]

    operations = [
        migrations.RunPython(create_labour_group, remove_labour_group),
    ]
