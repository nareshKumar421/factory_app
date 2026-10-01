"""Documents is now QA Reports: the lead's group and the labels follow.

* **QC Documents Lead** becomes **QA Reports Lead**; its members and grants stay.
  ("Production QC", the line QC staff's group, keeps its name, as in 0071.)
* The four permissions' stored names say "QA report"; codenames are unchanged.
  Django never rewrites an existing ``auth_permission`` row's name, so this does.
* The Meta label and the print-document key's label change with them — state
  only, no table is touched, so the data step can run in the same migration.
"""

from django.db import migrations, models

OLD_LEAD, NEW_LEAD = "QC Documents Lead", "QA Reports Lead"

NAMES = {
    "can_view_production_qc_entries": (
        "Can view QC document entries", "Can view QA report entries",
    ),
    "can_fill_production_qc_entries": (
        "Can fill and correct QC document entries", "Can fill and correct QA report entries",
    ),
    "can_approve_production_qc_entries": (
        "Can approve QC document entries", "Can approve QA report entries",
    ),
    "can_manage_production_qc_parameters": (
        "Can manage QC document types", "Can manage QA report types",
    ),
}


def _rename(apps, schema_editor, *, group_from, group_to, name_index):
    db = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    # Leave it alone if the new name is somehow taken already.
    if not Group.objects.using(db).filter(name=group_to).exists():
        Group.objects.using(db).filter(name=group_from).update(name=group_to)
    for codename, names in NAMES.items():
        Permission.objects.using(db).filter(
            codename=codename, content_type__app_label="quality_control"
        ).update(name=names[name_index])


def forwards(apps, schema_editor):
    _rename(apps, schema_editor, group_from=OLD_LEAD, group_to=NEW_LEAD, name_index=1)


def backwards(apps, schema_editor):
    _rename(apps, schema_editor, group_from=NEW_LEAD, group_to=OLD_LEAD, name_index=0)


class Migration(migrations.Migration):

    dependencies = [
        ('quality_control', '0071_rename_production_qc_to_documents'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='productionqcentry',
            options={'ordering': ['-checked_at', '-id'], 'permissions': [('can_view_production_qc_entries', 'Can view QA report entries'), ('can_fill_production_qc_entries', 'Can fill and correct QA report entries'), ('can_approve_production_qc_entries', 'Can approve QA report entries'), ('can_manage_production_qc_parameters', 'Can manage QA report types')]},
        ),
        migrations.AlterField(
            model_name='qcprintdocument',
            name='document_key',
            field=models.CharField(choices=[('RAW_MATERIAL_INSPECTION', 'Arrival Slip Inspection Print'), ('QC_PARAMETERS', 'Arrival Slip QC Parameters Print'), ('PRODUCTION_QC_SHEET', 'QA Report Sheet')], max_length=60),
        ),
        migrations.RunPython(forwards, backwards),
    ]
