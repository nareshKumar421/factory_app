"""Grant the audit-log permission to the address that actually exists.

0057 aimed at ``tajinder@jivo.in``. No such account exists -- the address on
record is ``tajinderjit@jivo.in`` -- so that migration found nobody and
granted nothing, silently, which is the failure mode a lookup-by-email grant
always risks.

This corrects the address. 0057 is left as applied rather than edited: a
migration that has already run on the live database is history, and rewriting
it would only mislead anyone reading the tree later. On a fresh database 0057
still no-ops harmlessly and this migration does the real work.

If the intended reader ever changes, add a new migration rather than editing
this one, and check afterwards that the grant actually landed -- an email that
does not match is indistinguishable from success here.
"""

from django.db import migrations

AUDIT = "can_view_document_file_audit"

AUDIT_READERS = ["tajinderjit@jivo.in"]


def forwards(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias

    app_config = global_apps.get_app_config("quality_control")
    if app_config.models_module is not None:
        create_permissions(app_config, verbosity=0, using=db)

    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model("accounts", "User")

    permission = (
        Permission.objects.using(db)
        .filter(codename=AUDIT, content_type__app_label="quality_control")
        .first()
    )
    if permission is None:
        return

    for email in AUDIT_READERS:
        user = User.objects.using(db).filter(email__iexact=email).first()
        if user:
            user.user_permissions.add(permission)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model("accounts", "User")

    permission = (
        Permission.objects.using(db)
        .filter(codename=AUDIT, content_type__app_label="quality_control")
        .first()
    )
    if permission is None:
        return

    for email in AUDIT_READERS:
        user = User.objects.using(db).filter(email__iexact=email).first()
        if user:
            user.user_permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0057_grant_document_file_audit_permission"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
