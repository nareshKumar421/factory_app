"""Grant warehouse.can_view_fg_receipt to every group that can view a run.

The production run screen loads the run's FG receipts, so any user who can view
a production run must be able to read FG receipts. This backfills existing
groups (including custom ones such as "factory head") that hold
production_execution.can_view_production_run. Idempotent, and it never grants
can_view_bom_request, so the Warehouse module stays hidden from production.
"""
from django.db import migrations


def grant(apps, schema_editor):
    from django.contrib.auth.management import create_permissions
    from django.apps import apps as global_apps

    create_permissions(global_apps.get_app_config("warehouse"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    run_view = Permission.objects.filter(
        content_type__app_label="production_execution",
        codename="can_view_production_run",
    ).first()
    view_fg = Permission.objects.filter(
        content_type__app_label="warehouse",
        codename="can_view_fg_receipt",
    ).first()
    if not run_view or not view_fg:
        return

    for group in Group.objects.filter(permissions=run_view).distinct():
        group.permissions.add(view_fg)


def noop(apps, schema_editor):
    # Not reversible: we can't tell which groups already had the permission.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0010_bom_fg_store_group"),
        ("production_execution", "0002_create_production_execution_group"),
    ]

    operations = [
        migrations.RunPython(grant, noop),
    ]
