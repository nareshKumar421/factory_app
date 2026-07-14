"""Grant the new ``can_select_dispatch_bills`` permission to everyone who already
has Dispatch / Plan access, so the Bill Selection page works with no manual setup.

Mirrors 0006 (which added ``can_link_dispatch_vehicle``): every group holding a
legacy dispatch permission gets the new one, and — to cover users who hold the
legacy permission directly rather than via a group — those users get it too.
"""
from django.db import migrations


NEW_PERMISSION = ("can_select_dispatch_bills", "Can select bills for dispatch planning")
LEGACY_PERMISSION_CODENAMES = (
    "can_view_dispatch_plans",
    "can_link_dispatch_vehicle",
)


def grant_select_dispatch_bills(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    content_type, _ = ContentType.objects.get_or_create(
        app_label="dispatch_plans",
        model="dispatchplan",
    )
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename=NEW_PERMISSION[0],
        defaults={"name": NEW_PERMISSION[1]},
    )

    legacy_permissions = Permission.objects.filter(
        content_type=content_type,
        codename__in=LEGACY_PERMISSION_CODENAMES,
    )

    # Groups that already grant dispatch access → give them the new permission.
    for group in Group.objects.filter(permissions__in=legacy_permissions).distinct():
        group.permissions.add(permission)

    # Users who hold a legacy permission DIRECTLY (not via a group) → grant directly,
    # so no existing dispatch user loses access to the new page.
    for user in User.objects.filter(user_permissions__in=legacy_permissions).distinct():
        user.user_permissions.add(permission)


def revoke_select_dispatch_bills(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    try:
        content_type = ContentType.objects.get(
            app_label="dispatch_plans", model="dispatchplan"
        )
        permission = Permission.objects.get(
            content_type=content_type, codename=NEW_PERMISSION[0]
        )
    except (ContentType.DoesNotExist, Permission.DoesNotExist):
        return

    for group in Group.objects.filter(permissions=permission):
        group.permissions.remove(permission)
    for user in User.objects.filter(user_permissions=permission):
        user.user_permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("dispatch_plans", "0017_alter_dispatchplan_options_selecteddispatchbill"),
    ]

    operations = [
        migrations.RunPython(grant_select_dispatch_bills, revoke_select_dispatch_bills),
    ]
