from django.db import migrations


def create_production_planning_group(apps, schema_editor):
    """Create 'production_planning' group with all module permissions."""
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, created = Group.objects.get_or_create(name='production_planning')

    permissions = Permission.objects.filter(
        content_type__app_label='production_planning'
    )
    group.permissions.set(permissions)


def remove_production_planning_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='production_planning').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('production_planning', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(
            create_production_planning_group,
            remove_production_planning_group
        ),
    ]
