"""Fit the sheets that already exist into the flow that now has two desks.

Every live sheet in the books was raised under the old rule, where issuing WAS
approving: the dispatch date was typed on the form and the invoice was stamped in
the same breath. Those sheets are therefore approved, and saying so is not a
guess — an unapproved sheet is one with no dispatch date, and none of these has
that.

``approved_by`` is deliberately left empty. Nobody approved them; the step did
not exist. ``approved_at`` takes the moment they were issued, because under the
old rule that is when the dispatch date was fixed and SAP was told.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    BillSummary = apps.get_model("dispatch_plans", "BillSummary")

    # ``submitted_at`` defaults to now, so every existing sheet would otherwise
    # claim it was sent to the warehouse the moment this migration ran — and the
    # queue sorts on it. Every row here predates the column, so all of them take
    # the moment they were issued.
    BillSummary.objects.all().update(submitted_at=models.F("issued_at"))

    BillSummary.objects.filter(status="GENERATED").update(
        status="APPROVED", approved_at=models.F("issued_at")
    )
    # A picked sheet was approved on the way through, and its record should say
    # so rather than showing a pick with nothing before it.
    BillSummary.objects.filter(status="PICKED", approved_at__isnull=True).update(
        approved_at=models.F("issued_at")
    )


def backwards(apps, schema_editor):
    BillSummary = apps.get_model("dispatch_plans", "BillSummary")
    # PENDING_APPROVAL and REJECTED have no pre-split equivalent; they collapse
    # into the one status the old flow had for a sheet that exists.
    BillSummary.objects.filter(
        status__in=["APPROVED", "PRINTED", "PENDING_APPROVAL", "REJECTED"]
    ).update(status="GENERATED")


class Migration(migrations.Migration):

    dependencies = [
        ("dispatch_plans", "0028_alter_billsummary_options_billsummary_approved_at_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
