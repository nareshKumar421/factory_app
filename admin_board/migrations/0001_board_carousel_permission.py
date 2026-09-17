"""A right of its own for the board carousel.

WHY THIS EXISTS
The carousel (``/dashboards/carousel``) rotates the Admin, Plant and Logistics
control boards on an unattended wall screen. Until now it was gated on the UNION
of those three boards' rights — ten of them — because none of the boards mints a
right of its own. That is defensible for a person who already reads those
boards; it is wrong for a SCREEN. A wall display had to be handed ten rights
covering stock, non-moving, the production plan, dispatch, factory expense and
four WMS reads, every one of which also opens the operational report behind it.

This mints ``admin_board.can_view_board_carousel`` so a display login can hold
exactly one permission, see exactly one page, and reach nothing else.

WHAT IT DOES NOT DO
It grants nothing. The permission row is created; who holds it is an
administrator's decision through ``setup_dashboard_groups``, not a migration's.
It also creates no table and alters none — this is a data migration over
``auth_permission`` and nothing else, which is why it is safe to apply to the
live database on its own.

WHERE IT HANGS
``admin_board`` has no model, so the right hangs off a synthetic content type,
the same technique ``gate_core.0059`` uses for the gate dashboard. The app was
chosen because it is the closest thing the backend has to an owner for "the
control boards"; there is no ``dashboards`` app, the frontend module of that
name being a grouping over a dozen backends.
"""

from django.db import migrations

CODENAME = "can_view_board_carousel"
NAME = "Can view the board carousel"


def add_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct, _ = ContentType.objects.get_or_create(app_label="admin_board", model="adminboard")
    Permission.objects.get_or_create(
        codename=CODENAME,
        content_type=ct,
        defaults={"name": NAME},
    )


def remove_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct = ContentType.objects.filter(app_label="admin_board", model="adminboard").first()
    if ct is not None:
        Permission.objects.filter(codename=CODENAME, content_type=ct).delete()


class Migration(migrations.Migration):
    """First migration in this app: it has no models, only this right."""

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(add_permission, remove_permission),
    ]
