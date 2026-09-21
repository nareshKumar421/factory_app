"""The right that opens the dashboard builder.

WHAT IT GRANTS
The editor, and nothing else. ``board_builder.can_build_dashboards`` lets
somebody create a board, arrange cards on it and publish it. It grants no
data: the palette an author is offered is filtered to the cards whose FEED
rights they already hold (``board_builder/permissions.py``), and every figure
on every board they build is read through ``control_boards.sections``, which
withholds per card for each reader in turn.

So this is a product right sitting on top of the existing data rights, not
beside them. Handing it to somebody widens what they can ARRANGE and never
what they can see -- which is the property that makes it safe to give out
fairly freely, and the property to check before ever honouring it anywhere
near a query.

WHERE IT HANGS
Off the app's own ``customboard`` content type, which this app has because
unlike the other board apps it owns tables. No synthetic type needed.

WHAT IT DOES NOT DO
Grant itself to anybody. The row is created; who holds it is an
administrator's decision through a group, not a migration's.
"""

from django.db import migrations

CODENAME = "can_build_dashboards"
NAME = "Can build dashboards"


def add_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct, _ = ContentType.objects.get_or_create(
        app_label="board_builder", model="customboard"
    )
    Permission.objects.get_or_create(
        codename=CODENAME,
        content_type=ct,
        defaults={"name": NAME},
    )


def remove_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct = ContentType.objects.filter(
        app_label="board_builder", model="customboard"
    ).first()
    if ct is not None:
        Permission.objects.filter(codename=CODENAME, content_type=ct).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("board_builder", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(add_permission, remove_permission),
    ]
