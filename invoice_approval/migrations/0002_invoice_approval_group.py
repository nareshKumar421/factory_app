"""Create/refresh the 'Invoice Approval' auth group for the SAP-backed module.

This module replaces the retired ``oms`` proxy app but keeps the same group name
and permission codenames (only the app label changes), so existing approvers keep
working without re-assignment:

1. create the new ``invoice_approval.*`` permissions and add them to the group;
2. mirror every group/user assignment of the old ``oms.*`` permissions onto the
   new ones (covers direct user grants and any extra groups an admin made);
3. delete the old ``oms`` permissions and content types.

The old ``oms_invoice_approval_audit`` table is left in place for manual cleanup
(its rows reference OMS invoice-log ids, an id-space that would collide with SAP
WddCodes if copied over). Permissions are created explicitly (content type +
codename) so this works on a fresh ``migrate`` before the auth post_migrate
signal has created them. Mirrors ``marketplace/migrations/0002_marketplace_group.py``.
"""
from django.db import migrations

# codename -> human label
CUSTOM_PERMS = {
    "view_invoice": "Can view SAP invoices for approval",
    "approve_invoice": "Can approve or reject SAP invoices",
}


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    User = apps.get_model("accounts", "User")

    ct, _ = ContentType.objects.get_or_create(
        app_label="invoice_approval", model="invoiceapprovalaudit"
    )
    new_perms = {}
    for codename, label in CUSTOM_PERMS.items():
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": label}
        )
        new_perms[codename] = perm

    group, _ = Group.objects.get_or_create(name="Invoice Approval")
    group.permissions.add(*new_perms.values())

    # Carry over assignments from the retired oms.* permissions, then delete them.
    old_perms = Permission.objects.filter(
        content_type__app_label="oms", codename__in=CUSTOM_PERMS
    )
    for old in old_perms:
        new = new_perms[old.codename]
        for g in Group.objects.filter(permissions=old):
            g.permissions.add(new)
        for user in User.objects.filter(user_permissions=old):
            user.user_permissions.add(new)
    old_perms.delete()
    ContentType.objects.filter(app_label="oms").delete()


def remove_group(apps, schema_editor):
    # Reverse only detaches the new permissions; the group itself (and any old
    # oms.* state this migration deleted) is not resurrected.
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="invoice_approval", codename__in=CUSTOM_PERMS
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoice_approval", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("accounts", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_group, remove_group)]
