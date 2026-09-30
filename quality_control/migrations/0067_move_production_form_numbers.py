"""The form numbers typed onto parameter types move to Print Documents, where
every printed form's number now lives (Master Data > Print Documents).

Its own migration, between the schema changes on either side: Postgres will not
alter a table in the same transaction as rows written to it are still being
checked ("pending trigger events").
"""

from django.db import migrations


def move_type_codes_to_print_documents(apps, schema_editor):
    ProductionParameterType = apps.get_model("quality_control", "ProductionParameterType")
    QCPrintDocument = apps.get_model("quality_control", "QCPrintDocument")
    for parameter_type in ProductionParameterType.objects.exclude(document_code=""):
        QCPrintDocument.objects.get_or_create(
            company_id=parameter_type.company_id,
            document_key="PRODUCTION_QC_SHEET",
            production_parameter_type=parameter_type,
            defaults={"document_id": parameter_type.document_code},
        )


def move_them_back(apps, schema_editor):
    ProductionParameterType = apps.get_model("quality_control", "ProductionParameterType")
    QCPrintDocument = apps.get_model("quality_control", "QCPrintDocument")
    for document in QCPrintDocument.objects.filter(document_key="PRODUCTION_QC_SHEET"):
        ProductionParameterType.objects.filter(pk=document.production_parameter_type_id).update(
            document_code=document.document_id
        )
    QCPrintDocument.objects.filter(document_key="PRODUCTION_QC_SHEET").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0066_print_documents_for_production_forms"),
    ]

    operations = [
        migrations.RunPython(move_type_codes_to_print_documents, move_them_back),
    ]
