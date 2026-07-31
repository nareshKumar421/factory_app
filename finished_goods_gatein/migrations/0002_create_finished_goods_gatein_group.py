from django.db import migrations


def create_group(apps, schema_editor):
    """Create 'finished_goods_gatein' group with all FG gate-in permissions."""
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group, _ = Group.objects.get_or_create(name="finished_goods_gatein")
    permissions = Permission.objects.filter(
        content_type__app_label="finished_goods_gatein"
    )
    group.permissions.set(permissions)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="finished_goods_gatein").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finished_goods_gatein", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
