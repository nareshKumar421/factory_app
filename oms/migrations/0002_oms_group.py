"""Create the 'Invoice Approval' auth group bundling the OMS proxy's permissions.

Permissions are created explicitly (content type + codename) so this works on a
fresh ``migrate`` before the auth post_migrate signal has created them. Mirrors
``marketplace/migrations/0002_marketplace_group.py``.
"""
from django.db import migrations

# codename -> (model_name for content type, human label)
CUSTOM_PERMS = {
    "view_invoice": ("invoiceapprovalaudit", "Can view OMS invoices for approval"),
    "approve_invoice": ("invoiceapprovalaudit", "Can approve or reject OMS invoices"),
}


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name="Invoice Approval")
    perms = []
    for codename, (model_name, label) in CUSTOM_PERMS.items():
        ct, _ = ContentType.objects.get_or_create(app_label="oms", model=model_name)
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": label}
        )
        perms.append(perm)
    group.permissions.add(*perms)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="Invoice Approval").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_group, remove_group)]
