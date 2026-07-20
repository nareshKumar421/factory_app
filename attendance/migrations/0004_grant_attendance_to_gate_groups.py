from django.db import migrations

# The attendance group self-heals here; the gate operator groups get access to
# the whole submodule (view/mark/delete records + manage the employee register).
SELF_GROUP = "attendance"
GATE_GROUPS = ["person_gatein", "gate_core", "gate_in"]


def grant_attendance_perms(apps, schema_editor):
    # On a fresh database, model permissions (and content types) are created by a
    # post_migrate signal that fires only AFTER every migration finishes — so they
    # may not exist yet while this data migration runs. Create them explicitly so
    # the group grants below actually have permissions to attach.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    app_config = global_apps.get_app_config("attendance")
    create_permissions(app_config, verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    perms = list(Permission.objects.filter(content_type__app_label="attendance"))
    for name in [SELF_GROUP, *GATE_GROUPS]:
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(*perms)


def revoke_attendance_perms(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    perms = list(Permission.objects.filter(content_type__app_label="attendance"))
    # Only pull the perms back off the gate groups on reverse; leave the
    # attendance group intact.
    for name in GATE_GROUPS:
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0003_remove_attendancerecord_unique_employee_date_attendance_and_more"),
    ]

    operations = [
        migrations.RunPython(grant_attendance_perms, revoke_attendance_perms),
    ]
