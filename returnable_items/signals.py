"""Auto-provision the module's auth groups on migrate.

Same mechanism as ``maintenance.signals.ensure_maintenance_groups``: groups are
not migrations, they are rebuilt from ``RETURNABLE_ROLE_PERMISSIONS`` every time
the app migrates, so adding a permission to the dict is enough to grant it.
"""

from .constants import RETURNABLE_ROLE_PERMISSIONS


def ensure_returnable_groups(sender, app_config, **kwargs):
    if app_config.label != "returnable_items":
        return

    from django.contrib.auth.models import Group, Permission

    permissions = Permission.objects.filter(content_type__app_label="returnable_items")
    permissions_by_code = {permission.codename: permission for permission in permissions}

    for group_name, codenames in RETURNABLE_ROLE_PERMISSIONS.items():
        group, _created = Group.objects.get_or_create(name=group_name)
        if codenames == "__all__":
            group.permissions.set(permissions)
            continue

        group.permissions.set(
            [
                permissions_by_code[codename]
                for codename in codenames
                if codename in permissions_by_code
            ]
        )
