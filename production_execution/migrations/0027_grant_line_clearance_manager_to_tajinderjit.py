from django.db import migrations
from django.db.models import Q

PERM_CODENAME = "can_manage_line_clearance"
PERM_NAME = "Can manage/override line clearance decisions"

# accounts.User keys on email (no username field). Match "tajinderjit" across the
# human-identifying fields so this works regardless of the exact email used,
# and safely no-ops in any database where that user does not exist.
USER_MATCH = (
    Q(email__icontains="tajinderjit")
    | Q(full_name__icontains="tajinderjit")
    | Q(employee_code__icontains="tajinderjit")
)


def _get_perm(apps):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    # The permission is declared on ProductionRun.Meta, so it belongs to that
    # content type. get_or_create so this works even before the post_migrate
    # signal has created the permission row.
    try:
        ct = ContentType.objects.get(app_label="production_execution", model="productionrun")
    except ContentType.DoesNotExist:
        return None
    perm, _ = Permission.objects.get_or_create(
        codename=PERM_CODENAME,
        content_type=ct,
        defaults={"name": PERM_NAME},
    )
    return perm


def grant(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    perm = _get_perm(apps)
    if perm is None:
        return
    for user in User.objects.filter(USER_MATCH):
        user.user_permissions.add(perm)


def revoke(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Permission = apps.get_model("auth", "Permission")
    for perm in Permission.objects.filter(codename=PERM_CODENAME):
        for user in User.objects.filter(USER_MATCH):
            user.user_permissions.remove(perm)


class Migration(migrations.Migration):

    dependencies = [
        ("production_execution", "0026_alter_productionrun_options_lineclearancedecisionlog"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
