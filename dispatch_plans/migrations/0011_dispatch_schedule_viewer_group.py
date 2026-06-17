from django.db import migrations


# Dedicated read-only role for the warehouse Dispatch Schedule page. Holds only
# can_view_dispatch_schedule, so warehouse staff can view the schedule without
# any dispatch-plans edit/link capability or the full dispatch dashboard.
# Membership is assigned operationally per environment.
GROUP_NAME = "Dispatch Schedule Viewer"
PERMISSION_CODENAME = "can_view_dispatch_schedule"


def create_dispatch_schedule_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    content_type = ContentType.objects.filter(
        app_label="dispatch_plans",
        model="dispatchplan",
    ).first()
    if content_type is None:
        return

    permission = Permission.objects.filter(
        content_type=content_type,
        codename=PERMISSION_CODENAME,
    ).first()
    if permission is None:
        return

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set([permission])


def delete_dispatch_schedule_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dispatch_plans", "0010_add_dispatch_schedule_permission"),
    ]

    operations = [
        migrations.RunPython(
            create_dispatch_schedule_group,
            delete_dispatch_schedule_group,
        ),
    ]
