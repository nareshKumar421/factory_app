"""One board-read right per feed in ``control_boards.feeds.FEEDS``.

WHAT THIS DOES
Creates one ``auth_permission`` row per feed. It creates NO table and alters
none -- this is a data migration over ``auth_permission`` and the content type it
hangs off, which is why it is safe to apply to the live database on its own.

WHAT IT DOES NOT DO
It grants nothing. Not one user's access changes when this runs. Who holds these
rights is an administrator's decision through ``setup_dashboard_groups``, never a
migration's.

WHERE THEY HANG
``control_boards`` has no model, so the rights hang off a synthetic content type
``(control_boards, boardfeed)`` -- the same technique ``admin_board.0001`` uses
for the board carousel and ``gate_core.0059`` for the gate dashboard.
``boardfeed`` is not a model and never will be; it is a stable pair for
``auth_permission.content_type_id`` to point at.

WHY THE CATALOGUE IS READ RATHER THAN COPIED
The codenames are looped out of ``FEEDS`` instead of being written out here, so a
feed added to the catalogue cannot be left without a permission row. The cost is
the usual one for importing application code into a migration: if a feed is ever
REMOVED from the catalogue, this migration stops knowing how to reverse its row.
That is the right trade -- feed rights are additive in practice, and a dropped
row is a follow-up migration's job, where it can be reviewed on its own.
"""

from django.db import migrations

from control_boards.feeds import APP_LABEL, CONTENT_TYPE_MODEL, FEEDS


def add_permissions(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct, _ = ContentType.objects.get_or_create(
        app_label=APP_LABEL, model=CONTENT_TYPE_MODEL
    )
    for feed in FEEDS.values():
        Permission.objects.get_or_create(
            codename=feed.codename,
            content_type=ct,
            defaults={"name": feed.label},
        )


def remove_permissions(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct = ContentType.objects.filter(
        app_label=APP_LABEL, model=CONTENT_TYPE_MODEL
    ).first()
    if ct is None:
        return
    Permission.objects.filter(
        codename__in=[f.codename for f in FEEDS.values()], content_type=ct
    ).delete()


class Migration(migrations.Migration):
    """First migration in this app: it has no models, only these rights."""

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(add_permissions, remove_permissions)]
