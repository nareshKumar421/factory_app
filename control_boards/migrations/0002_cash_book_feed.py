"""The board-read right for the ``cash_book`` feed.

WHAT THIS DOES
Creates one ``auth_permission`` row, ``control_boards.can_read_cash_book_feed``,
hanging off the same synthetic ``(control_boards, boardfeed)`` content type
0001 established. It creates no table and alters none, so it is safe to apply to
the live database on its own.

WHAT IT DOES NOT DO
It grants nothing, to nobody. Who holds the right is an administrator's decision
through ``setup_dashboard_groups``, never a migration's. In particular the
carousel display login does not acquire the accounts board by this running.

WHY IT NAMES THE FEED INSTEAD OF LOOPING THE CATALOGUE
0001 loops ``FEEDS`` and is idempotent, so re-running its logic would also do
the job. It is not reused here on purpose: a migration that means "add the row
this change introduces" should still mean that when the catalogue has grown
another five feeds, and a second catalogue-wide loop in the history makes the
two indistinguishable in a review. The code is spelled out, and so is its
reversal.
"""

from django.db import migrations

CODENAME = "can_read_cash_book_feed"
LABEL = "Board feed: cash book"
APP_LABEL = "control_boards"
CONTENT_TYPE_MODEL = "boardfeed"


def add_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct, _ = ContentType.objects.get_or_create(
        app_label=APP_LABEL, model=CONTENT_TYPE_MODEL
    )
    Permission.objects.get_or_create(
        codename=CODENAME, content_type=ct, defaults={"name": LABEL}
    )


def remove_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    ct = ContentType.objects.filter(
        app_label=APP_LABEL, model=CONTENT_TYPE_MODEL
    ).first()
    if ct is None:
        return
    Permission.objects.filter(codename=CODENAME, content_type=ct).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("control_boards", "0001_feed_rights"),
    ]

    operations = [
        migrations.RunPython(add_permission, remove_permission),
    ]
