"""Grant the marketplace out-gate check permission to the Marketplace group AND the
gate_core group, so both marketplace operators and gate personnel can use the Gate
page. Idempotent — creates the permission explicitly if post_migrate hasn't yet."""
from django.db import migrations

PERM = ("gate_check", "marketplacedispatch", "Can perform the marketplace out-gate check")
GROUPS = ["Marketplace", "gate_core"]


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    codename, model_name, label = PERM
    ct, _ = ContentType.objects.get_or_create(app_label="marketplace", model=model_name)
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename=codename, defaults={"name": label}
    )
    for name in GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.add(perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    codename = PERM[0]
    perm = Permission.objects.filter(codename=codename, content_type__app_label="marketplace").first()
    if perm:
        for name in GROUPS:
            group = Group.objects.filter(name=name).first()
            if group:
                group.permissions.remove(perm)


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0028_alter_marketplacedispatch_options"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(grant, revoke)]
