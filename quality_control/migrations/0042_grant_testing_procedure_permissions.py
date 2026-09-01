"""Grant the new testing-procedure permissions to the existing QC groups.

Without this, 0041 creates the tables but nobody holds
``can_view_testing_procedures``, so the QC "Documents" page stays hidden from
every user until someone assigns the permission by hand.

QA management (and the factory head) get view + manage; the chemist and store
QC groups get view only -- they read procedures, they do not author them.
"""

from django.db import migrations

VIEW = "can_view_testing_procedures"
MANAGE = "can_manage_testing_procedures"

# Group names as they exist in this deployment. Missing names are skipped, so
# this is safe on an environment that does not have all of them.
MANAGER_GROUPS = ["qc_manager", "QC Manager", "factory head"]
VIEWER_GROUPS = ["qc_store", "qc_chemist"]


def grant(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias

    # Custom permissions are normally created by a post_migrate signal -- that
    # is, *after* this data migration runs -- so the rows would not exist yet.
    # Force them into existence first.
    app_config = global_apps.get_app_config("quality_control")
    if app_config.models_module is not None:
        create_permissions(app_config, verbosity=0, using=db)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    def permission(codename):
        return (
            Permission.objects.using(db)
            .filter(codename=codename, content_type__app_label="quality_control")
            .first()
        )

    view_perm = permission(VIEW)
    manage_perm = permission(MANAGE)
    if view_perm is None or manage_perm is None:
        # Nothing sensible to do -- leave the grant to an administrator.
        return

    for name in MANAGER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view_perm, manage_perm)

    for name in VIEWER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view_perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    db = schema_editor.connection.alias

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
        ("quality_control", "0041_testingprocedure_testingproceduresection_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
