from django.db import migrations


# Permissions a dock operator needs to fill docking details on the sales
# dispatch flow (Steps 1-3): edit transport documents and upload photos /
# documents. View + create already reach dock operators via direct grants;
# these two were the only ones missing, which blocked the Step 3 page.
#
# These are added to the "dispatch" group rather than the gate's
# "Sales Dispatch Out" group to keep the dock and gate roles separate. The
# perms are inert for dispatch members who lack view/create, so this grants
# no new docking access to non-dock members of the group.
DOCK_PERMISSIONS = [
    "can_edit_sales_dispatch_out",
    "can_upload_sales_dispatch_photo",
]

DISPATCH_GROUP_NAME = "dispatch"


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


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0032_sales_dispatch_challan_weight"),
    ]

    operations = [
        migrations.RunPython(
            add_dock_permissions_to_dispatch_group,
            remove_dock_permissions_from_dispatch_group,
        ),
    ]
