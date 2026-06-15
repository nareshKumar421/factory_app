from django.db import migrations


def create_docking_admin_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    try:
        ct = ContentType.objects.get(
            app_label="docking_admin", model="dockingscanskiprequest"
        )
    except ContentType.DoesNotExist:
        return

    def perms(*codenames):
        return Permission.objects.filter(content_type=ct, codename__in=codenames)

    # Admins who review (view + approve/reject) skip requests.
    approver, _ = Group.objects.get_or_create(name="Docking Approver")
    approver.permissions.set(
        perms("can_view_docking_scan_skip", "can_approve_docking_scan_skip")
    )

    # Operators who can raise a skip request from the scanning page.
    operator, _ = Group.objects.get_or_create(name="Docking Scan Operator")
    operator.permissions.set(perms("can_request_docking_scan_skip"))


def reverse(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(
        name__in=["Docking Approver", "Docking Scan Operator"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("docking_admin", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_docking_admin_groups, reverse),
    ]
