"""Grant ``backdate_delivery_note`` to the Marketplace group.

The permission gates cutting a delivery note into a previous month. It is granted
to the same group that already holds ``confirm_dispatch`` — the only people who can
cut a note at all (3 users today), plus superusers implicitly.

Deliberately not left ungranted: a permission no group holds means the operator who
needs it gets a silent 403, which is exactly how the Service GRPO outage ran for six
weeks. Narrow it later by moving it to a supervisor group if that is wanted; the
check is a plain ``user.has_perm`` and follows whatever group holds it.

Idempotent — creates the permission explicitly if post_migrate hasn't yet.
"""
from django.db import migrations

PERM = (
    "backdate_delivery_note",
    "marketplacedispatch",
    "Can cut a marketplace delivery note into a previous month",
)
GROUPS = ["Marketplace"]


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
    perm = Permission.objects.filter(
        codename=PERM[0], content_type__app_label="marketplace"
    ).first()
    if perm:
        for name in GROUPS:
            group = Group.objects.filter(name=name).first()
            if group:
                group.permissions.remove(perm)


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0032_alter_marketplacedispatch_options_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(grant, revoke)]
