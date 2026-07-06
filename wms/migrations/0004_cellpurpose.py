"""Add the CellPurpose collection and grant its perms to the WMS Admin group.

Cell purposes (walkable path, damaged goods, storage…) are a structural,
admin-authored collection like zones/locations, so only the ``WMS Admin`` group
gets write access; operators remain scan-workflow only.
"""
import django.db.models.deletion
from django.db import migrations, models

WMS_ADMIN = 'WMS Admin'
CODENAMES = ['add_cellpurpose', 'change_cellpurpose', 'delete_cellpurpose']


def grant_admin_perms(apps, schema_editor):
    # The post_migrate hook that creates model permissions has not run yet for
    # this migration, so create them explicitly for the wms app first.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    wms_config = global_apps.get_app_config('wms')
    create_permissions(wms_config, verbosity=0)

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    admin_group = Group.objects.filter(name=WMS_ADMIN).first()
    if admin_group is None:
        return  # groups not seeded yet -> 0003 will grant on its own run
    perms = Permission.objects.filter(
        content_type__app_label='wms', codename__in=CODENAMES
    )
    admin_group.permissions.add(*perms)


def revoke_admin_perms(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    admin_group = Group.objects.filter(name=WMS_ADMIN).first()
    if admin_group is None:
        return
    perms = Permission.objects.filter(
        content_type__app_label='wms', codename__in=CODENAMES
    )
    admin_group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ('wms', '0003_wms_access_groups'),
        ('company', '0003_alter_usercompany_role'),
        ('auth', '0001_initial'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CellPurpose',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('record_id', models.CharField(db_index=True, help_text="Client-generated record id (UUID, or 'wms-settings').", max_length=64)),
                ('data', models.JSONField(default=dict, help_text='The full camelCase record document as authored by the frontend.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='company.company')),
            ],
            options={
                'ordering': ['created_at'],
                'abstract': False,
                'unique_together': {('company', 'record_id')},
            },
        ),
        migrations.RunPython(grant_admin_perms, revoke_admin_perms),
    ]
