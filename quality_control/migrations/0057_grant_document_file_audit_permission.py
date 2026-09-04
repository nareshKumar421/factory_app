"""Grant the QA Procedures audit-log permission.

Same shape as 0048, with one deliberate difference: this is granted to a named
person rather than to a group. The point of the log is that the people being
logged are not the people who read it, so it does not belong on ``qc_manager``
alongside the upload rights -- until QA decides otherwise, exactly one account
can see who changed a controlled procedure.

Adding someone later means appending to ``AUDIT_READERS`` in a new migration
(or granting the permission through the admin); this one is written to be safe
to re-run and to no-op cleanly on a database where the account does not exist,
so it does not block a fresh dev or CI setup.
"""

from django.db import migrations

AUDIT = "can_view_document_file_audit"

# By email, because that is the username field on ``accounts.User``.
AUDIT_READERS = ["tajinder@jivo.in"]


def forwards(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    db = schema_editor.connection.alias

    # Custom permissions are created by a post_migrate signal, i.e. after this
    # data migration runs, so force the rows into existence first.
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
        # Case-insensitive: the account may well have been created as
        # "Tajinder@jivo.in".
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
        ("quality_control", "0056_qcdocumentfileauditlog"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
