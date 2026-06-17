from django.db import migrations


# Read-only permission for the warehouse Dispatch Schedule page. Lets warehouse
# staff view scheduled dispatch plans (today / tomorrow / upcoming) without the
# full dispatch-plans dashboard or any edit/link capability.
NEW_PERMISSION = ("can_view_dispatch_schedule", "Can view Dispatch Schedule (read-only)")


def create_dispatch_schedule_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    content_type, _ = ContentType.objects.get_or_create(
        app_label="dispatch_plans",
        model="dispatchplan",
    )
    Permission.objects.get_or_create(
        content_type=content_type,
        codename=NEW_PERMISSION[0],
        defaults={"name": NEW_PERMISSION[1]},
    )


def remove_dispatch_schedule_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")

    try:
        content_type = ContentType.objects.get(
            app_label="dispatch_plans",
            model="dispatchplan",
        )
        permission = Permission.objects.get(
            content_type=content_type,
            codename=NEW_PERMISSION[0],
        )
    except (ContentType.DoesNotExist, Permission.DoesNotExist):
        return

    for group in Group.objects.filter(permissions=permission):
        group.permissions.remove(permission)
    permission.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dispatch_plans", "0009_dispatchplan_service_grpo_defaults"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="dispatchplan",
            options={
                "ordering": ["-updated_at", "-created_at"],
                "permissions": [
                    ("can_view_dispatch_plans", "Can view Dispatch Plans dashboard"),
                    ("can_edit_dispatch_plans", "Can edit Dispatch Plans bookings"),
                    ("can_link_dispatch_vehicle", "Can link dispatch vehicles"),
                    ("can_view_dispatch_schedule", "Can view Dispatch Schedule (read-only)"),
                ],
            },
        ),
        migrations.RunPython(
            create_dispatch_schedule_permission,
            remove_dispatch_schedule_permission,
        ),
    ]
