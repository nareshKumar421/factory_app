"""Add the sheet-import + warehouse-issue permissions to the 'Marketplace' group.

Permissions created explicitly (content type + codename) so this works on a fresh
``migrate`` before the auth post_migrate signal (mirrors ``0002``).
"""
from django.db import migrations

NEW_PERMS = {
    "import_orders": ("orderimportbatch", "Can import marketplace order sheets"),
    "view_batch": ("orderimportbatch", "Can view marketplace import batches"),
    "send_issue_request": ("marketplaceissuerequest", "Can send marketplace issue request to warehouse"),
    "review_issue_request": ("marketplaceissuerequest", "Can approve/reject marketplace issue request"),
    "issue_materials": ("marketplaceissuerequest", "Can issue materials for a marketplace request"),
    "receive_issue": ("marketplaceissuerequest", "Can receive issued marketplace materials"),
}


def add_perms(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name="Marketplace")
    perms = []
    for codename, (model_name, label) in NEW_PERMS.items():
        ct, _ = ContentType.objects.get_or_create(app_label="marketplace", model=model_name)
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": label}
        )
        perms.append(perm)
    group.permissions.add(*perms)


def remove_perms(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(codename__in=NEW_PERMS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0003_combodefinition_image_marketplaceorder_address_line1_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(add_perms, remove_perms)]
