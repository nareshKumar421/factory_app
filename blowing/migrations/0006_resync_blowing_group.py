from django.db import migrations


def resync_group(apps, schema_editor):
    """Re-sync the catch-all 'blowing' group with all blowing permissions so
    existing installs pick up the new lifecycle/warehouse permissions."""
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    group, _ = Group.objects.get_or_create(name='blowing')
    perms = Permission.objects.filter(content_type__app_label='blowing')
    group.permissions.set(perms)


class Migration(migrations.Migration):
    dependencies = [
        ('blowing', '0005_alter_blowingrun_options_and_more'),
    ]
    operations = [
        migrations.RunPython(resync_group, migrations.RunPython.noop),
    ]
