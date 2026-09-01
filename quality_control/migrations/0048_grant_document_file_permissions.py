"""Grant the PDF-library permissions to the existing QC groups.

Same shape as 0042 / 0044: without this the page exists but is invisible,
because nobody holds ``can_view_document_files``. QA management can upload
and retire; the chemist and store groups can read.
"""

from django.db import migrations

VIEW = "can_view_document_files"
MANAGE = "can_manage_document_files"

MANAGER_GROUPS = ["qc_manager", "QC Manager", "factory head"]
VIEWER_GROUPS = ["qc_chemist", "qc_store"]


def forwards(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias

    # Custom permissions are created by a post_migrate signal, i.e. after this
    # data migration runs, so force the rows into existence first.
    app_config = global_apps.get_app_config("quality_control")
    if app_config.models_module is not None:
        create_permissions(app_config, verbosity=0, using=db)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    def perm(codename):
        return (
            Permission.objects.using(db)
            .filter(codename=codename, content_type__app_label="quality_control")
            .first()
        )

    view, manage = perm(VIEW), perm(MANAGE)
    if view is None or manage is None:
        return

    for name in MANAGER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view, manage)

    for name in VIEWER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    perms = list(
        Permission.objects.using(db).filter(
            codename__in=[VIEW, MANAGE], content_type__app_label="quality_control"
        )
    )
    if not perms:
        return
    for group in Group.objects.using(db).filter(
        name__in=MANAGER_GROUPS + VIEWER_GROUPS
    ):
        group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0047_qcdocumentfile"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
