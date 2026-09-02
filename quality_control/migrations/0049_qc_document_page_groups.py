"""Create one permission group per QC document page.

Until now the three areas' permissions were only granted to the broad QC
groups (``qc_manager``, ``qc_chemist``, ``qc_store``, ``factory head``), so
giving somebody access to just one page meant hand-picking permissions.

These groups make each page assignable on its own:

* **QC Procedures**    -- the controlled testing procedures (SOPs).
* **QC Documents**     -- the fillable record sheets.
* **QC PDF Documents** -- the PDF library.

Idempotent: the groups are fetched or created, and ``permissions.add`` is a
no-op when the permission is already attached. The existing broad grants are
left untouched -- these groups are an addition, not a replacement.
"""

from django.db import migrations

GROUPS = {
    "QC Procedures": [
        "can_view_testing_procedures",
        "can_manage_testing_procedures",
    ],
    "QC Documents": [
        "can_view_qc_records",
        "can_fill_qc_records",
        "can_approve_qc_records",
    ],
    "QC PDF Documents": [
        "can_view_document_files",
        "can_manage_document_files",
    ],
}


def forwards(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias

    # Custom permissions are created by a post_migrate signal -- after this
    # runs -- so force the rows into existence before looking them up.
    app_config = global_apps.get_app_config("quality_control")
    if app_config.models_module is not None:
        create_permissions(app_config, verbosity=0, using=db)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    for group_name, codenames in GROUPS.items():
        permissions = list(
            Permission.objects.using(db).filter(
                codename__in=codenames, content_type__app_label="quality_control"
            )
        )
        # Refuse to create a half-empty group: better to leave it absent than
        # to hand an administrator a group that silently grants nothing.
        if len(permissions) != len(codenames):
            continue
        group, _ = Group.objects.using(db).get_or_create(name=group_name)
        group.permissions.add(*permissions)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")

    for group_name in GROUPS:
        group = Group.objects.using(db).filter(name=group_name).first()
        if not group:
            continue
        # Keep a group somebody has been assigned to; just strip the grants,
        # so reversing never silently removes a person's group membership.
        if group.user_set.exists():
            group.permissions.clear()
        else:
            group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0048_grant_document_file_permissions"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
