"""
Give every company a branch master with one row in it, marked default.

The branch field is meant to arrive pre-answered: the hire form pre-selects the
default, and a master with no rows would leave it with nothing to select and
nothing to say about why. So each company starts with a branch named after
itself -- ``Jivo Oil`` for ``JIVO_OIL``, which is the one the factory directory
actually sits in and the one the requirement named.

Existing employees are deliberately **not** back-filled onto it. 252 people
predate the field, and writing a branch onto all of them would assert something
nobody checked; a blank branch is visibly unanswered, which is the state HR can
actually act on. New joiners get the default from
:func:`employee_hierarchy.services.create_employee`.

Idempotent, so it survives a re-run against a database where somebody has
already set the master up by hand.
"""

from django.db import migrations


def seed_default_branches(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    Branch = apps.get_model("employee_hierarchy", "Branch")

    for company in Company.objects.all():
        if Branch.objects.filter(company=company).exists():
            continue
        Branch.objects.create(
            company=company,
            code=company.code[:30],
            name=company.name[:100],
            description="Created with the branch master. Rename or retire it freely.",
            status="ACTIVE",
            is_default=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("employee_hierarchy", "0007_branch_employee_branch_and_more"),
    ]

    operations = [
        # Irreversible only in the sense that reversing 0007 drops the table
        # and the rows with it, which is the right outcome anyway.
        migrations.RunPython(seed_default_branches, migrations.RunPython.noop),
    ]
