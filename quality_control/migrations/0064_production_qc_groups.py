"""Grant the production QC permissions.

* **Production QC** -- the line QC staff (the group already holds the
  line-clearance QC perms): see, make and correct checks.
* **Production QC Lead** -- new: see and approve checks, and keep the
  parameter types. Nobody is in it yet; an administrator adds the leads.
* **qc_manager** -- the QAM holds every QC permission, these included.

Idempotent, like 0049: the groups are fetched or created and
``permissions.add`` is a no-op for a grant already there. Custom permissions
are created by a post_migrate signal that fires after this runs, so they are
forced into existence first; a group whose permissions still cannot be found is
left alone rather than given half its grants.
"""

from django.db import migrations

VIEW = "can_view_production_qc_entries"
FILL = "can_fill_production_qc_entries"
APPROVE = "can_approve_production_qc_entries"
MANAGE = "can_manage_production_qc_parameters"

GROUPS = {
    "Production QC": [VIEW, FILL],
    "Production QC Lead": [VIEW, APPROVE, MANAGE],
    "qc_manager": [VIEW, FILL, APPROVE, MANAGE],
}

NEW_GROUPS = {"Production QC Lead"}


def forwards(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias
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
        if len(permissions) != len(codenames):
            continue
        group, _ = Group.objects.using(db).get_or_create(name=group_name)
        group.permissions.add(*permissions)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    for group_name, codenames in GROUPS.items():
        group = Group.objects.using(db).filter(name=group_name).first()
        if not group:
            continue
        group.permissions.remove(
            *Permission.objects.using(db).filter(
                codename__in=codenames, content_type__app_label="quality_control"
            )
        )
        # Keep a group somebody has been put in; only drop the one made here
        # when it is still empty.
        if group_name in NEW_GROUPS and not group.user_set.exists():
            group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0063_production_qc"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
