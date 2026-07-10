"""Create the "BOM & FG Store" group and split BOM/FG permissions.

BOM Requests and FG Receipts previously rode on ``production_execution``
permissions, which leaked the Warehouse module to production users. They now
have dedicated ``warehouse.*`` permissions:

* "BOM & FG Store" (new group) — warehouse staff who view/approve/issue BOM
  requests and receive/post FG receipts. Membership assigned operationally.
* ``production_execution`` group — keeps only the *production-side* actions
  (submit BOM, create/view FG receipts from the run screen). Deliberately NOT
  granted ``can_view_bom_request`` so the Warehouse module stays hidden from it.
"""
from django.db import migrations

STORE_GROUP_NAME = "BOM & FG Store"

STORE_CODENAMES = [
    "can_view_bom_request",
    "can_approve_bom_request",
    "can_issue_materials",
    "can_view_fg_receipt",
    "can_receive_fg",
    "can_post_fg_to_sap",
]

PRODUCTION_CODENAMES = [
    "can_create_bom_request",
    "can_create_fg_receipt",
    "can_view_fg_receipt",
]


def apply(apps, schema_editor):
    # post_migrate hasn't created the new model permissions yet on a fresh run.
    from django.contrib.auth.management import create_permissions
    from django.apps import apps as global_apps

    create_permissions(global_apps.get_app_config("warehouse"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    store_group, _ = Group.objects.get_or_create(name=STORE_GROUP_NAME)
    store_perms = Permission.objects.filter(
        content_type__app_label="warehouse",
        codename__in=STORE_CODENAMES,
    )
    store_group.permissions.add(*store_perms)

    prod_group = Group.objects.filter(name="production_execution").first()
    if prod_group:
        prod_perms = Permission.objects.filter(
            content_type__app_label="warehouse",
            codename__in=PRODUCTION_CODENAMES,
        )
        prod_group.permissions.add(*prod_perms)


def revert(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    Group.objects.filter(name=STORE_GROUP_NAME).delete()

    prod_group = Group.objects.filter(name="production_execution").first()
    if prod_group:
        prod_perms = Permission.objects.filter(
            content_type__app_label="warehouse",
            codename__in=PRODUCTION_CODENAMES,
        )
        prod_group.permissions.remove(*prod_perms)


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0009_alter_bomrequest_options_and_more"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(apply, revert),
    ]
