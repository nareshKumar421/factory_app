from django.db import migrations


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name='blowing')
    perms = Permission.objects.filter(content_type__app_label='blowing')
    group.permissions.set(perms)


class Migration(migrations.Migration):
    dependencies = [
        ('blowing', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(create_group, migrations.RunPython.noop),
    ]
