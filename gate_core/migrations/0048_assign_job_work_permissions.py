"""Grant the new Job Work permissions to the existing ``gate_core`` group.

Job Work / Oil Refining previously rode on ``production_execution`` run
permissions, which leaked the whole Gate module to production users. It now has
its own ``gate_core`` permissions; this assigns them to gate operators so they
keep access while production users lose the Gate module.
"""
from django.db import migrations

JOB_WORK_CODENAMES = [
    "can_view_job_work",
    "can_create_job_work",
    "can_complete_job_work",
]


def assign(apps, schema_editor):
    # On a fresh database the post_migrate hook that creates model permissions
    # has not run yet, so create them explicitly for gate_core first.
    from django.contrib.auth.management import create_permissions
    from django.apps import apps as global_apps

    create_permissions(global_apps.get_app_config("gate_core"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group = Group.objects.filter(name="gate_core").first()
    if not group:
        return
    perms = Permission.objects.filter(
        content_type__app_label="gate_core",
        codename__in=JOB_WORK_CODENAMES,
    )
    group.permissions.add(*perms)


def unassign(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group = Group.objects.filter(name="gate_core").first()
    if not group:
        return
    perms = Permission.objects.filter(
        content_type__app_label="gate_core",
        codename__in=JOB_WORK_CODENAMES,
    )
    group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0047_alter_jobworkgatein_options"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(assign, unassign),
    ]
