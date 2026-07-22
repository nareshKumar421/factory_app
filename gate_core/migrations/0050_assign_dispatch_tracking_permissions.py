"""Grant the new Dispatch Tracking permissions to existing dispatch groups.

Anyone who can already view Sales Dispatch Out (the dispatch operators) gets the
post-dispatch tracking permissions too, so the new page is visible to them
without a manual permission change.
"""
from django.db import migrations

TRACKING_CODENAMES = [
    "can_view_dispatch_tracking",
    "can_update_dispatch_tracking",
]


def assign(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    # On a fresh DB the post_migrate hook that creates model permissions hasn't run
    # yet, so create them explicitly for gate_core first.
    create_permissions(global_apps.get_app_config("gate_core"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    tracking = list(
        Permission.objects.filter(
            content_type__app_label="gate_core",
            codename__in=TRACKING_CODENAMES,
        )
    )
    if not tracking:
        return

    dispatch_groups = (
        Group.objects.filter(
            permissions__content_type__app_label="gate_core",
            permissions__codename="can_view_sales_dispatch_out",
        )
        .distinct()
    )
    for group in dispatch_groups:
        group.permissions.add(*tracking)


def unassign(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    perms = Permission.objects.filter(
        content_type__app_label="gate_core",
        codename__in=TRACKING_CODENAMES,
    )
    for group in Group.objects.all():
        group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0049_truckdispatchupdate"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(assign, unassign),
    ]
