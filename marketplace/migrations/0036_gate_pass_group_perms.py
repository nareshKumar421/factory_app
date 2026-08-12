"""Grant the gate-pass permissions to the groups that actually work the gate.

The gate person raises the trip, weighs it, prints the pass and marks it out, so
all five go to "marketplace gate"; the marketplace operators get them too, since
they raise trips when the gate is unattended.

Modelled on 0029 (gate_check): idempotent, reversible, and it creates the
permissions explicitly rather than assuming post_migrate has already run — a
fresh database applies migrations before post_migrate creates model permissions,
so relying on them existing makes this fail on a clean install.
"""
from django.db import migrations

MODEL = "marketplacegatepass"
PERMS = [
    ("can_view_mp_gate_pass", "Can view marketplace gate passes"),
    ("can_manage_mp_gate_pass", "Can create and edit marketplace gate passes"),
    ("can_weigh_mp_gate_pass", "Can record marketplace gate pass weighment"),
    ("can_print_mp_gate_pass", "Can print a marketplace gatepass"),
    ("can_dispatch_mp_gate_pass", "Can mark a marketplace gate pass out"),
]
GROUPS = ["marketplace gate", "Marketplace"]


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct, _ = ContentType.objects.get_or_create(app_label="marketplace", model=MODEL)
    for name in GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        for codename, label in PERMS:
            perm, _ = Permission.objects.get_or_create(
                content_type=ct, codename=codename, defaults={"name": label}
            )
            group.permissions.add(perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    for name in GROUPS:
        group = Group.objects.filter(name=name).first()
        if not group:
            continue
        for codename, _label in PERMS:
            perm = Permission.objects.filter(
                codename=codename, content_type__app_label="marketplace"
            ).first()
            if perm:
                group.permissions.remove(perm)


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0035_marketplacedispatch_sap_posted_lines"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(grant, revoke)]
