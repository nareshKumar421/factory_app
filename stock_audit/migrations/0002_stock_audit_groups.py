"""The stock audit's two groups.

* ``Stock Auditor``         -- counts: sees the audit, enters counts. Not SAP's
  quantity, so a count is of the floor rather than a copy of SAP's figure.
* ``Stock Audit Manager``   -- runs the audit: every right, including SAP's
  quantity and the difference.

Seeing SAP's figure on its own (``can_view_audit_sap_qty``) is granted per user
or in another group, for an auditor who is trusted to see it.
"""
from django.db import migrations

AUDITOR = 'Stock Auditor'
MANAGER = 'Stock Audit Manager'
AUDITOR_CODENAMES = ['can_view_stock_audit', 'can_count_stock_audit']
MANAGER_CODENAMES = AUDITOR_CODENAMES + ['can_view_audit_sap_qty', 'can_manage_stock_audit']


def create_groups(apps, schema_editor):
    # On a fresh database the post_migrate hook that creates model permissions
    # has not run yet, so create them explicitly for this app first.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config('stock_audit'), verbosity=0)

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    perms = Permission.objects.filter(content_type__app_label='stock_audit')

    auditor, _ = Group.objects.get_or_create(name=AUDITOR)
    auditor.permissions.add(*perms.filter(codename__in=AUDITOR_CODENAMES))
    manager, _ = Group.objects.get_or_create(name=MANAGER)
    manager.permissions.add(*perms.filter(codename__in=MANAGER_CODENAMES))


def remove_groups(apps, schema_editor):
    apps.get_model('auth', 'Group').objects.filter(name__in=[AUDITOR, MANAGER]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('stock_audit', '0001_initial'),
        ('auth', '0001_initial'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
