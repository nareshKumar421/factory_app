"""Create the 'AR Invoices' auth group for the sales-invoice entry module.

Permissions are created explicitly (content type + codename) so this works on a
fresh ``migrate`` before the auth post_migrate signal has created them. Mirrors
``ap_invoice/migrations/0002_ap_invoice_group.py``.
"""
from django.db import migrations

CUSTOM_PERMS = {
    "view_ar_invoice_posting": "Can view A/R invoice postings",
    "create_ar_invoice_posting": "Can create and post A/R invoices",
}

GROUP_NAME = "AR Invoices"


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct, _ = ContentType.objects.get_or_create(
        app_label="ar_invoice", model="arinvoiceposting"
    )
    perms = []
    for codename, label in CUSTOM_PERMS.items():
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": label}
        )
        perms.append(perm)

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.add(*perms)


def remove_group(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="ar_invoice", codename__in=CUSTOM_PERMS
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ar_invoice", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_group, remove_group)]
