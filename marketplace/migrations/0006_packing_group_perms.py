"""Add the packing permissions to the 'Marketplace' group (mirrors 0004)."""
from django.db import migrations

NEW_PERMS = {
    "view_packing": ("marketplacepacking", "Can view marketplace packing"),
    "pack_order": ("marketplacepacking", "Can pack marketplace orders"),
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
        ("marketplace", "0005_marketplacepacking_marketplacepackbarcode"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(add_perms, remove_perms)]
