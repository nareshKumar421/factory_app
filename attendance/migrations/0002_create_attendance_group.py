from django.db import migrations


def create_attendance_group(apps, schema_editor):
    """Create 'attendance' group with all attendance app permissions."""
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group, _ = Group.objects.get_or_create(name="attendance")

    permissions = Permission.objects.filter(content_type__app_label="attendance")
    group.permissions.set(permissions)


def remove_attendance_group(apps, schema_editor):
    """Remove 'attendance' group."""
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="attendance").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_attendance_group, remove_attendance_group),
    ]
