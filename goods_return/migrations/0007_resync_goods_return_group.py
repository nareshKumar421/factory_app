from django.db import migrations


def resync_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name='goods_return')
    perms = Permission.objects.filter(content_type__app_label='goods_return')
    group.permissions.set(perms)


class Migration(migrations.Migration):
    dependencies = [
        ('goods_return', '0006_alter_goodsreturn_options_and_more'),
    ]
    operations = [
        migrations.RunPython(resync_group, migrations.RunPython.noop),
    ]
