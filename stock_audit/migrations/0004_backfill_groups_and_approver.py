"""Fill the new fields on audits taken before them, and add the approver's group.

* Lines copied before the SAP item group name was kept get the name of their
  category's group (106/105/102 carry the same name in every company).
* ``Stock Audit Approver`` -- approves or rejects completed audits, and sees
  SAP's figure to do it. ``Stock Audit Manager`` also gets approving and
  posting to SAP.
"""
from django.db import migrations

GROUP_NAMES = {
    'RM': 'RAW MATERIAL',
    'PM': 'PACKAGING MATERIAL',
    'FG': 'FINISHED',
    'OTHER': 'OTHER',
}
APPROVER = 'Stock Audit Approver'
MANAGER = 'Stock Audit Manager'
APPROVER_CODENAMES = ['can_view_stock_audit', 'can_view_audit_sap_qty', 'can_approve_stock_audit']
MANAGER_EXTRA = ['can_approve_stock_audit', 'can_post_stock_audit_to_sap']


def forwards(apps, schema_editor):
    StockAuditLine = apps.get_model('stock_audit', 'StockAuditLine')
    for category, name in GROUP_NAMES.items():
        StockAuditLine.objects.filter(category=category, item_group_name='').update(
            item_group_name=name)

    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config('stock_audit'), verbosity=0)
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    perms = Permission.objects.filter(content_type__app_label='stock_audit')

    approver, _ = Group.objects.get_or_create(name=APPROVER)
    approver.permissions.add(*perms.filter(codename__in=APPROVER_CODENAMES))
    manager, _ = Group.objects.get_or_create(name=MANAGER)
    manager.permissions.add(*perms.filter(codename__in=MANAGER_EXTRA))


def backwards(apps, schema_editor):
    apps.get_model('auth', 'Group').objects.filter(name=APPROVER).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('stock_audit', '0003_approval_and_sap_posting'),
        ('auth', '0001_initial'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
