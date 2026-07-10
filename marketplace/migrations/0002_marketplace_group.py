"""Create the 'Marketplace' auth group bundling the app's custom permissions.

Permissions are created explicitly (content type + codename) so this works on a
fresh ``migrate`` before the auth post_migrate signal has created them.
"""
from django.db import migrations

# codename -> (model_name for content type, human label)
CUSTOM_PERMS = {
    "view_dispatch": ("marketplacedispatch", "Can view marketplace dispatch"),
    "add_dispatch": ("marketplacedispatch", "Can create marketplace dispatch"),
    "scan_dispatch": ("marketplacedispatch", "Can scan marketplace dispatch"),
    "confirm_dispatch": ("marketplacedispatch", "Can confirm marketplace dispatch"),
    "cancel_dispatch": ("marketplacedispatch", "Can cancel marketplace dispatch"),
    "view_reconciliation": ("marketplacedispatch", "Can view marketplace reconciliation"),
    "view_return": ("marketplacereturn", "Can view marketplace return"),
    "add_return": ("marketplacereturn", "Can create marketplace return"),
    "submit_return": ("marketplacereturn", "Can submit marketplace return"),
    "view_master": ("skumapping", "Can view marketplace master data"),
    "change_master": ("skumapping", "Can change marketplace master data"),
}


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name="Marketplace")
    perms = []
    for codename, (model_name, label) in CUSTOM_PERMS.items():
        ct, _ = ContentType.objects.get_or_create(app_label="marketplace", model=model_name)
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": label}
        )
        perms.append(perm)
    group.permissions.add(*perms)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="Marketplace").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_group, remove_group)]
