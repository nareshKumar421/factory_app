"""Share the QA procedures that were uploaded before sharing existed.

0053 made ``QCDocumentFile.company`` nullable so a document can be shared
across every company, and new uploads are created that way. Documents filed
*before* that still carry the company that uploaded them, so they stay
invisible to the others -- which is not what a QA procedure library is for.

This nulls the company on the live ones, making the whole library visible
everywhere.

Two deliberate limits:

* Retired rows are left as they are. They are history, and re-scoping them
  could resurrect a code clash against a live document.
* A document is skipped if its code is already held by a shared document,
  which would break the shared-code uniqueness constraint. Nothing is
  destroyed to force the merge; such a row stays company-scoped.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    QCDocumentFile = apps.get_model("quality_control", "QCDocumentFile")

    scoped = (
        QCDocumentFile.objects.using(db)
        .filter(is_active=True, company__isnull=False)
        .order_by("id")
    )

    for document in scoped:
        if document.document_code:
            already_shared = (
                QCDocumentFile.objects.using(db)
                .filter(
                    is_active=True,
                    company__isnull=True,
                    document_code=document.document_code,
                )
                .exists()
            )
            if already_shared:
                # Another document already owns this code in the shared
                # library; leave this one where it is for a human to reconcile.
                continue

        document.company = None
        document.save(update_fields=["company"])


def backwards(apps, schema_editor):
    """Not reversible: the company each document came from is not recorded
    anywhere else, so there is nothing to restore it from. Leaving them
    shared is the honest no-op."""


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0058_fix_document_file_audit_reader_email"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
