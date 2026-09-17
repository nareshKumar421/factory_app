"""
Hand the new attendance rights to the groups that should already have them.

Two tiers, and the split is the module's whole access story (see
``attendance/permissions.py``):

* **Viewing** the daily sheet goes to the gate groups and the attendance group
  -- the people who already see who came through the gate -- plus HR.
* **Overriding** a status goes to HR alone. It contradicts a machine, on a day
  that has closed, and payroll is run from the result.

Groups are looked up by name and skipped when absent, so this runs on a fresh
database (where none of them exist yet) and on production (where they all do).
"""

from django.db import migrations

VIEW_GROUPS = [
    "attendance",
    "gate_core",
    "person_gatein",
    "gate_in",
    "HR",
    "HR Administrator",
]
#: Correcting attendance is an HR act. Deliberately short.
OVERRIDE_GROUPS = ["HR", "HR Administrator"]

VIEW_PERMS = ["can_view_daily_attendance", "can_export_attendance"]
OVERRIDE_PERMS = ["can_override_attendance_status", "can_sync_attendance"]


def grant(apps, schema_editor):
    # On a fresh database the post_migrate signal that creates model permissions
    # has not fired yet, so create them explicitly -- same reason as 0004.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config("attendance"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    def perms(codenames):
        return list(
            Permission.objects.filter(
                content_type__app_label="attendance", codename__in=codenames
            )
        )

    for names, codenames in ((VIEW_GROUPS, VIEW_PERMS), (OVERRIDE_GROUPS, OVERRIDE_PERMS)):
        found = perms(codenames)
        if not found:
            continue
        for name in names:
            group = Group.objects.filter(name=name).first()
            if group:
                group.permissions.add(*found)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    found = list(
        Permission.objects.filter(
            content_type__app_label="attendance",
            codename__in=VIEW_PERMS + OVERRIDE_PERMS,
        )
    )
    for name in set(VIEW_GROUPS + OVERRIDE_GROUPS):
        group = Group.objects.filter(name=name).first()
        if group and found:
            group.permissions.remove(*found)


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0005_attendancepermission_alter_attendancerecord_employee_and_more"),
    ]

    operations = [migrations.RunPython(grant, revoke)]
