"""Fold the per-company copies of a record form into one shared form.

0044 seeded the NMW form once per company, so an identical sheet exists three
times and editing it means editing every copy. Now that a form can be shared
(``company = NULL``), the duplicates are collapsed: the oldest copy of each
document code becomes the shared form, filled sheets are repointed onto it,
and the redundant copies are removed.

Deliberately conservative -- a duplicate is only deleted when nothing has
been captured against its parameters. ``RecordValue.parameter`` is PROTECTed,
so deleting a form that has captured cells would fail; such a copy is left
alone, company-scoped, rather than risking a filled sheet.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    RecordTemplate = apps.get_model("quality_control", "RecordTemplate")
    QCRecord = apps.get_model("quality_control", "QCRecord")
    RecordValue = apps.get_model("quality_control", "RecordValue")

    codes = (
        RecordTemplate.objects.using(db)
        .filter(is_active=True)
        .exclude(document_code="")
        .values_list("document_code", flat=True)
        .distinct()
    )

    for code in list(codes):
        copies = list(
            RecordTemplate.objects.using(db)
            .filter(is_active=True, document_code=code)
            .order_by("id")
        )
        if not copies:
            continue

        # Oldest copy wins and becomes the shared form.
        keeper = copies[0]
        if keeper.company_id is not None:
            keeper.company = None
            keeper.save(update_fields=["company"])

        for duplicate in copies[1:]:
            # Move any filled sheets onto the keeper. The per-day uniqueness
            # includes the company, so sheets from different plants on the
            # same date do not collide.
            QCRecord.objects.using(db).filter(template=duplicate).update(
                template=keeper
            )

            has_values = (
                RecordValue.objects.using(db)
                .filter(parameter__section__template=duplicate)
                .exists()
            )
            if has_values:
                # Cannot remove it without destroying captured readings.
                continue
            duplicate.delete()


def backwards(apps, schema_editor):
    """Not reversible in a meaningful way.

    The per-company copies were identical and have been merged; recreating
    them would invent rows rather than restore anything. Leaving the shared
    form in place is the honest no-op -- the schema migration before this one
    is what actually reverses.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0054_remove_recordtemplate_uq_record_template_company_code_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
