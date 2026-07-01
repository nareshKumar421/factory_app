"""
Create the WMS access groups and assign their permissions.

* ``WMS Admin``    — full control of every WMS collection.
* ``WMS Operator`` — operational writes only: create/update/delete pallets &
  inventory and *add* movements (the audit log is append-only, so operators
  never change/delete movements). Everything else is read-only for them (reads
  are not permission-gated in the API).

The API (``wms/views.py``) enforces these model permissions, so membership in
these groups is what grants write access.
"""
from django.db import migrations

WMS_ADMIN = 'WMS Admin'
WMS_OPERATOR = 'WMS Operator'

# Operator may run the scan workflows: pallets & inventory full CRUD, plus
# appending movements. No change/delete on movements; no writes to structural
# collections (warehouses/zones/locations/materials/templates/settings).
OPERATOR_CODENAMES = [
    'add_pallet', 'change_pallet', 'delete_pallet',
    'add_inventory', 'change_inventory', 'delete_inventory',
    'add_movement',
]


def create_groups(apps, schema_editor):
    # On a fresh database the post_migrate hook that creates model permissions
    # has not run yet, so create them explicitly for the wms app first.
    from django.contrib.auth.management import create_permissions
    from django.apps import apps as global_apps

    wms_config = global_apps.get_app_config('wms')
    create_permissions(wms_config, verbosity=0)

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    wms_perms = Permission.objects.filter(content_type__app_label='wms')

    admin_group, _ = Group.objects.get_or_create(name=WMS_ADMIN)
    admin_group.permissions.set(list(wms_perms))

    operator_group, _ = Group.objects.get_or_create(name=WMS_OPERATOR)
    operator_group.permissions.set(list(wms_perms.filter(codename__in=OPERATOR_CODENAMES)))


def remove_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=[WMS_ADMIN, WMS_OPERATOR]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('wms', '0002_dashboard'),
        ('auth', '0001_initial'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
