"""Grant ``maintenance.view_asset`` to everyone who has Fire-store access.

The Fire store / Fire report pages call the shared ``/maintenance/options/``
endpoint, which is gated on ``maintenance.view_asset`` (CanViewAsset). A user
who only has fire permissions therefore gets a 403 on options. This command
grants ``view_asset`` to every Group and User that already holds any fire-view
permission, so those lookups succeed.

Idempotent — safe to run repeatedly and on any environment (prod included):

    python manage.py grant_fire_asset_view
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

FIRE_VIEW_CODENAMES = [
    "can_view_fire",
    "can_view_fire_report",
    "can_view_fire_issue",
]
TARGET_CODENAME = "view_asset"


class Command(BaseCommand):
    help = "Grant maintenance.view_asset to every group/user that has Fire-store access."

    def handle(self, *args, **options):
        try:
            asset_perm = Permission.objects.get(
                content_type__app_label="maintenance",
                codename=TARGET_CODENAME,
            )
        except Permission.DoesNotExist:
            self.stderr.write(
                self.style.ERROR("maintenance.view_asset not found — run migrations first.")
            )
            return

        fire_perm_ids = list(
            Permission.objects.filter(
                content_type__app_label="maintenance",
                codename__in=FIRE_VIEW_CODENAMES,
            ).values_list("id", flat=True)
        )
        if not fire_perm_ids:
            self.stderr.write(self.style.ERROR("No fire-view permissions found — run migrations first."))
            return

        group_updates = 0
        for group in Group.objects.filter(permissions__in=fire_perm_ids).distinct():
            if not group.permissions.filter(pk=asset_perm.pk).exists():
                group.permissions.add(asset_perm)
                group_updates += 1
                self.stdout.write(f"  + group '{group.name}' granted view_asset")

        user_model = get_user_model()
        user_updates = 0
        for user in user_model.objects.filter(user_permissions__in=fire_perm_ids).distinct():
            if not user.user_permissions.filter(pk=asset_perm.pk).exists():
                user.user_permissions.add(asset_perm)
                user_updates += 1
                self.stdout.write(
                    f"  + user '{getattr(user, 'email', None) or user.pk}' granted view_asset"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Groups updated: {group_updates}, users updated: {user_updates}."
            )
        )
