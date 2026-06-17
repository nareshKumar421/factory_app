from django.db import migrations


# Retires the interim grant from migration 0033. Dock operators now live in the
# dedicated "Sales Dispatch Dock" group (migration 0034), so the "dispatch"
# group no longer needs the docking edit/upload permissions. This returns
# "dispatch" to being purely dispatch-plans / service-GRPO / AP-invoice.
DOCK_PERMISSIONS = [
    "can_edit_sales_dispatch_out",
    "can_upload_sales_dispatch_photo",
]

DISPATCH_GROUP_NAME = "dispatch"


def remove_dock_permissions_from_dispatch_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    SalesDispatchGateOut = apps.get_model("gate_core", "SalesDispatchGateOut")

    group = Group.objects.filter(name=DISPATCH_GROUP_NAME).first()
    if group is None:
        return

    content_type = ContentType.objects.get_for_model(SalesDispatchGateOut)
    permissions = list(
        Permission.objects.filter(
            content_type=content_type,
            codename__in=DOCK_PERMISSIONS,
        )
    )
    if permissions:
        group.permissions.remove(*permissions)


def add_dock_permissions_to_dispatch_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    SalesDispatchGateOut = apps.get_model("gate_core", "SalesDispatchGateOut")

    group = Group.objects.filter(name=DISPATCH_GROUP_NAME).first()
    if group is None:
        return

    content_type = ContentType.objects.get_for_model(SalesDispatchGateOut)
    permissions = list(
        Permission.objects.filter(
            content_type=content_type,
            codename__in=DOCK_PERMISSIONS,
        )
    )
    if permissions:
        group.permissions.add(*permissions)


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0034_sales_dispatch_dock_group"),
    ]

    operations = [
        migrations.RunPython(
            remove_dock_permissions_from_dispatch_group,
            add_dock_permissions_to_dispatch_group,
        ),
    ]
