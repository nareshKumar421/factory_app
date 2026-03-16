"""
Drop production_plan FK from ProductionRun and LineClearance,
add sap_doc_entry (SAP OWOR DocEntry) integer field to both.

Production planning is now managed entirely in SAP — no local plan model needed.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('production_execution', '0003_resource_tracking_qc_cost'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                -- ProductionRun: drop production_plan FK column
                -- CASCADE also drops the FK constraint and the unique index that includes it
                ALTER TABLE production_execution_productionrun
                    DROP COLUMN IF EXISTS production_plan_id CASCADE;

                -- ProductionRun: add sap_doc_entry
                ALTER TABLE production_execution_productionrun
                    ADD COLUMN IF NOT EXISTS sap_doc_entry integer NULL;

                -- LineClearance: drop production_plan FK column
                ALTER TABLE production_execution_lineclearance
                    DROP COLUMN IF EXISTS production_plan_id CASCADE;

                -- LineClearance: add sap_doc_entry
                ALTER TABLE production_execution_lineclearance
                    ADD COLUMN IF NOT EXISTS sap_doc_entry integer NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
