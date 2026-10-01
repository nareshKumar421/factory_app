"""Production QC is now Documents: rename the lead's group and the permission labels.

* **Production QC Lead** (made by 0064, document rights only) becomes
  **QC Documents Lead**; its members and grants stay.
* **Production QC** keeps its name: it is the line QC staff's group from before
  0064 and holds the line-clearance QC rights too.
* The four permissions keep their codenames; only the names stored in
  ``auth_permission`` change, since Django never rewrites an existing row's
  name when Meta's label changes.
"""

from django.db import migrations

OLD_LEAD, NEW_LEAD = "Production QC Lead", "QC Documents Lead"

NAMES = {
    "can_view_production_qc_entries": (
        "Can view production QC entries",
        "Can view QC document entries",
    ),
    "can_fill_production_qc_entries": (
        "Can make and correct production QC entries",
        "Can fill and correct QC document entries",
    ),
    "can_approve_production_qc_entries": (
        "Can approve production QC entries",
        "Can approve QC document entries",
    ),
    "can_manage_production_qc_parameters": (
        "Can manage production QC parameter types",
        "Can manage QC document types",
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
        ("quality_control", "0070_documents_not_tied_to_production"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
