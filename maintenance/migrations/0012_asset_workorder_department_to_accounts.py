"""Repoint Asset.department and MaintenanceWorkOrder.department from the
company-scoped maintenance.AssetDepartment to the global accounts.Department.

Existing rows hold AssetDepartment ids, which do not map to accounts.Department
ids. To migrate safely on any database we:
  1. add a temporary nullable FK to accounts.Department,
  2. remap each row by matching the old department name to an accounts.Department
     of the same name (creating it if missing),
  3. drop the old column, rename the temp column into place, and make it required.
"""

import django.db.models.deletion
from django.db import migrations, models


def remap_departments(apps, schema_editor):
    Asset = apps.get_model("maintenance", "Asset")
    WorkOrder = apps.get_model("maintenance", "MaintenanceWorkOrder")
    AssetDepartment = apps.get_model("maintenance", "AssetDepartment")
    Department = apps.get_model("accounts", "Department")

    cache = {}

    def resolve(old_department_id):
        if old_department_id in cache:
            return cache[old_department_id]
        old = AssetDepartment.objects.filter(pk=old_department_id).first()
        name = (old.name.strip() if old and old.name else "") or "General"
        dept = Department.objects.filter(name__iexact=name).first()
        if dept is None:
            dept = Department.objects.create(name=name)
        cache[old_department_id] = dept
        return dept

    for asset in Asset.objects.exclude(department__isnull=True).iterator():
        asset.department_new = resolve(asset.department_id)
        asset.save(update_fields=["department_new"])

    for work_order in WorkOrder.objects.exclude(department__isnull=True).iterator():
        work_order.department_new = resolve(work_order.department_id)
        work_order.save(update_fields=["department_new"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_cleanup_stale_permissions"),
        ("maintenance", "0011_alter_maintenancepermission_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="department_new",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="maintenance_assets_tmp",
                to="accounts.department",
            ),
        ),
        migrations.AddField(
            model_name="maintenanceworkorder",
            name="department_new",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="maintenance_work_orders_tmp",
                to="accounts.department",
            ),
        ),
        migrations.RunPython(remap_departments, migrations.RunPython.noop),
        migrations.RemoveField(model_name="asset", name="department"),
        migrations.RemoveField(model_name="maintenanceworkorder", name="department"),
        migrations.RenameField(
            model_name="asset",
            old_name="department_new",
            new_name="department",
        ),
        migrations.RenameField(
            model_name="maintenanceworkorder",
            old_name="department_new",
            new_name="department",
        ),
        migrations.AlterField(
            model_name="asset",
            name="department",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="maintenance_assets",
                to="accounts.department",
            ),
        ),
        migrations.AlterField(
            model_name="maintenanceworkorder",
            name="department",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="maintenance_work_orders",
                to="accounts.department",
            ),
        ),
    ]
