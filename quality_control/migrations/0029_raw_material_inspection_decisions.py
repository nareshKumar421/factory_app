from django.db import migrations, models


def backfill_decisions(apps, schema_editor):
    RawMaterialInspection = apps.get_model("quality_control", "RawMaterialInspection")

    final_to_decision = {
        "ACCEPTED": "APPROVED",
        "REJECTED": "REJECTED",
        "HOLD": "HOLD",
    }

    for inspection in RawMaterialInspection.objects.all().iterator():
        update_fields = []

        if not inspection.qa_chemist_decision and (
            inspection.qa_chemist_id
            or inspection.workflow_status in {"QA_CHEMIST_APPROVED", "QAM_APPROVED", "COMPLETED"}
        ):
            inspection.qa_chemist_decision = "APPROVED"
            update_fields.append("qa_chemist_decision")

        manager_decision = final_to_decision.get(inspection.final_status)
        if not inspection.qam_decision and manager_decision:
            inspection.qam_decision = manager_decision
            update_fields.append("qam_decision")

        if update_fields:
            inspection.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0028_qcprintdocument"),
    ]

    operations = [
        migrations.AddField(
            model_name="rawmaterialinspection",
            name="qa_chemist_decision",
            field=models.CharField(
                blank=True,
                choices=[
                    ("APPROVED", "Approved"),
                    ("HOLD", "Hold"),
                    ("REJECTED", "Rejected"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="rawmaterialinspection",
            name="qam_decision",
            field=models.CharField(
                blank=True,
                choices=[
                    ("APPROVED", "Approved"),
                    ("HOLD", "Hold"),
                    ("REJECTED", "Rejected"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="rawmaterialinspection",
            index=models.Index(fields=["qa_chemist_decision"], name="quality_con_qa_chem_c7f183_idx"),
        ),
        migrations.AddIndex(
            model_name="rawmaterialinspection",
            index=models.Index(fields=["qam_decision"], name="quality_con_qam_dec_7925b1_idx"),
        ),
        migrations.RunPython(backfill_decisions, migrations.RunPython.noop),
    ]
