"""A right of its own for the Gate wall board.

Until now the board was reachable by anyone holding any one of five unrelated
operational rights — view a PO receipt, view a gate entry, view a person entry,
view a sales dispatch gate-out. It had no permission of its own, so being allowed
to *do* a thing at the gate silently meant being allowed to *watch the whole
gate*. On live that added up to 41 of 107 active users, most of whom had never
been granted anything of the sort: 13 QC chemists reached it through
``raw_material_gatein.view_poreceipt`` alone.

This mints ``gate_core.can_view_gate_dashboard`` so the board can be granted and
revoked on its own. Nothing is granted here — the permission is created and the
dashboard groups that exist for this purpose pick it up via
``setup_dashboard_groups``; who ends up in those groups is an administrator's
decision, not a migration's.

Hung off the synthetic ``gate_core.gatecore`` content type, the same place the
app's other cross-cutting rights live (see ``0002_add_can_view_gate_entry``);
gate_core has no single model that owns "the gate" as a subject.
"""

from django.db import migrations

CODENAME = "can_view_gate_dashboard"
NAME = "Can view the gate dashboard"


def add_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct, _ = ContentType.objects.get_or_create(app_label="gate_core", model="gatecore")
    Permission.objects.get_or_create(
        codename=CODENAME,
        content_type=ct,
        defaults={"name": NAME},
    )


def remove_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct = ContentType.objects.filter(app_label="gate_core", model="gatecore").first()
    if ct is not None:
        Permission.objects.filter(codename=CODENAME, content_type=ct).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0058_dispatch_box_loose_split"),
    ]

    operations = [
        migrations.RunPython(add_permission, remove_permission),
    ]
