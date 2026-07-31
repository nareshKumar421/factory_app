from django.db import migrations


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name='goods_return')
    perms = Permission.objects.filter(content_type__app_label='goods_return')
    group.permissions.set(perms)


class Migration(migrations.Migration):
    dependencies = [
        ('goods_return', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(create_group, migrations.RunPython.noop),
    ]
