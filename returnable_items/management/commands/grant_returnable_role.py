"""Assign a user to one of the Returnable Items roles.

The roles (Django auth groups) are created and kept in sync automatically on
every migrate, from ``RETURNABLE_ROLE_PERMISSIONS`` in ``constants.py``. This
command just puts people in them.

    python manage.py grant_returnable_role head@jivo.in returnable_approver
    python manage.py grant_returnable_role guard@jivo.in returnable_gate
    python manage.py grant_returnable_role --list

Roles:
    returnable_department  raise, edit, submit, acknowledge, close, cancel
    returnable_approver    approve or reject a submitted pass (cannot raise one)
    returnable_gate        gate out, gate in, reject at gate
    returnable_admin       everything, including short close
    returnable_viewer      read-only + reports
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from returnable_items.constants import RETURNABLE_ROLE_PERMISSIONS

ROLES = sorted(RETURNABLE_ROLE_PERMISSIONS.keys())


class Command(BaseCommand):
    help = "Add a user to a Returnable Items role (auth group)."

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?", help="User email address")
        parser.add_argument("role", nargs="?", choices=ROLES, help="Role to grant")
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Remove the user from the role instead of adding them.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List the roles and who currently holds them.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list_roles()
            return

        email, role = options["email"], options["role"]
        if not email or not role:
            raise CommandError("Provide an email and a role, or use --list.")

        User = get_user_model()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user with email {email}.") from exc

        try:
            group = Group.objects.get(name=role)
        except Group.DoesNotExist as exc:
            raise CommandError(
                f"Role {role} does not exist. Run `manage.py migrate returnable_items` first."
            ) from exc

        if options["remove"]:
            user.groups.remove(group)
            self.stdout.write(self.style.WARNING(f"Removed {email} from {role}."))
            return

        user.groups.add(group)
        self.stdout.write(self.style.SUCCESS(f"Added {email} to {role}."))

        if user.is_superuser:
            self.stdout.write(
                "Note: this user is a superuser and already had every permission."
            )

    def _list_roles(self):
        for role in ROLES:
            group = Group.objects.filter(name=role).first()
            if not group:
                self.stdout.write(self.style.WARNING(f"{role}: not created — run migrate"))
                continue
            members = list(group.user_set.values_list("email", flat=True))
            self.stdout.write(
                f"{role} ({group.permissions.count()} perms): "
                + (", ".join(members) if members else "— nobody —")
            )
