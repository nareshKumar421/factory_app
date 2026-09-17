"""
The approve permission follows approval onto the entry, and a dead one goes.

``can_approve_cash_bunch`` was declared on the bunch in 0001. Approval moved
onto the entry in 0004, and 0006 -- which stripped the bunch of everything to
do with deciding -- carried the permission off with it. Nothing has declared it
since, so a database built from these migrations today would never create it,
and ``has_perm`` would quietly answer False for everybody but a superuser. It is
re-declared here on ``CashEntry``, where approval actually lives, under a name
that says what it guards: ``can_approve_cash_entries``.

``can_manage_cash_advances`` is simply dead. It was declared in 0003 and no view
has ever checked it -- handing out a float is cash leaving the box, which the
advance endpoints already guard as ordinary custodian work.

Both old rows are deleted rather than left lying about. A permission that grants
nothing is worse than a missing one: it shows up in the admin's picker looking
like a control, and whoever ticks it believes they have given something away.
Neither is held by any group, on any database, so nothing is taken from anyone.
"""

from django.db import migrations

RETIRED = ["can_approve_cash_bunch", "can_manage_cash_advances"]


def drop_retired_permissions(apps, schema_editor):
    """Delete the two permissions nothing declares or checks any more."""
    Permission = apps.get_model("auth", "Permission")
    doomed = Permission.objects.filter(
        content_type__app_label="cash_book", codename__in=RETIRED
    )
    # Django's own post_migrate hook creates permissions but never removes
    # them, so these rows outlive the model options that made them.
    doomed.delete()


def restore_retired_permissions(apps, schema_editor):
    """Put them back, so a rollback leaves the table as it found it.

    Recreated against the models they were declared on: the approval one on
    the bunch, the advance one on the advance entry.
    """
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    for codename, model, name in (
        (
            "can_approve_cash_bunch",
            "cashbunch",
            "Can approve or reject a bunch of cash entries",
        ),
        (
            "can_manage_cash_advances",
            "advanceentry",
            "Can give and take back cash advances",
        ),
    ):
        content_type = ContentType.objects.filter(
            app_label="cash_book", model=model
        ).first()
        if content_type is None:
            continue
        Permission.objects.get_or_create(
            content_type=content_type, codename=codename, defaults={"name": name}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("cash_book", "0006_bunch_is_a_batch_record"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="advanceentry",
            options={
                "ordering": ["-entry_date", "-id"],
                "verbose_name_plural": "Advance entries",
            },
        ),
        migrations.AlterModelOptions(
            name="cashentry",
            options={
                "ordering": ["id"],
                "permissions": [
                    ("can_view_cash_book", "Can view the cash book"),
                    (
                        "can_manage_cash_book",
                        "Can record, correct and cancel cash entries",
                    ),
                    (
                        "can_approve_cash_entries",
                        "Can approve or reject cash entries",
                    ),
                ],
                "verbose_name_plural": "Cash entries",
            },
        ),
        migrations.RunPython(
            drop_retired_permissions,
            restore_retired_permissions,
            elidable=False,
        ),
    ]
