from django.db import migrations


# Dedicated "dock operator" role for the sales dispatch / docking flow.
# Dock operators prepare a dispatch end-to-end (view + create the entry, scan
# boxes, fill transport documents, upload photos / documents) but do NOT print,
# commit, or dispatch the gatepass -- that is the gate stage, kept in the
# separate "Sales Dispatch Out" group.
#
# This group exists because dock operators were spread across unrelated groups
# ("dispatch", "barcode"), neither of which is dock-only. Membership is managed
# operationally (per environment); this migration only defines the role.
DOCK_GROUP_NAME = "Sales Dispatch Dock"

DOCK_PERMISSIONS = [
    "can_view_sales_dispatch_out",
    "can_create_sales_dispatch_out",
    "can_edit_sales_dispatch_out",
    "can_upload_sales_dispatch_photo",
]


def create_dock_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    SalesDispatchGateOut = apps.get_model("gate_core", "SalesDispatchGateOut")

    group, _ = Group.objects.get_or_create(name=DOCK_GROUP_NAME)

    content_type = ContentType.objects.get_for_model(SalesDispatchGateOut)
    permissions = list(
        Permission.objects.filter(
            content_type=content_type,
            codename__in=DOCK_PERMISSIONS,
        )
    )
    if permissions:
        group.permissions.set(permissions)


def delete_dock_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=DOCK_GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0033_dispatch_group_dock_permissions"),
    ]

    operations = [
        migrations.RunPython(create_dock_group, delete_dock_group),
    ]
