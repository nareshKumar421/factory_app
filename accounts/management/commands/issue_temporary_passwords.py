"""Issue temporary passwords to users who cannot log in yet.

    manage.py issue_temporary_passwords --without-password --dry-run
    manage.py issue_temporary_passwords --without-password --output /root/portal-passwords.csv
    manage.py issue_temporary_passwords --email someone@jivo.in --output /root/one.csv

``--without-password`` picks every active user with no usable password — the
SAP Portal users ``import_portal_users`` created. ``--email`` names users one by
one; a user who already has a password is skipped unless ``--reset-existing``.

The passwords go to ``--output`` only: a CSV (email, full name, temporary
password) readable by its owner alone. Nothing is printed, so nothing lands in
a terminal log. Each user must choose their own password at the next login.
See ``accounts/temporary_passwords.py``.
"""

import csv
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.temporary_passwords import issue


class Command(BaseCommand):
    help = "Issue temporary passwords (changed at first login) to users who have none."

    def add_arguments(self, parser):
        parser.add_argument("--without-password", action="store_true",
                            help="Every active user with no usable password (e.g. imported portal users).")
        parser.add_argument("--email", action="append", default=[], help="A user's email; repeat for more.")
        parser.add_argument("--reset-existing", action="store_true",
                            help="Also replace the password of a named user who already has one.")
        parser.add_argument("--output", help="CSV file to write the passwords to (created, mode 600).")
        parser.add_argument("--dry-run", action="store_true", help="List who would get one; change nothing.")

    def handle(self, *args, **options):
        users = self._users(options)
        if not users:
            self.stdout.write("Nobody to issue a temporary password to.")
            return
        for user in users:
            self.stdout.write(f"  {user.email}  ({user.full_name})")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Dry run: {len(users)} user(s) would get a temporary password."))
            return

        path = options["output"]
        if not path:
            raise CommandError("--output is required: the passwords are written only to that file.")
        if os.path.exists(path):
            raise CommandError(f"{path} already exists; choose a new file so no password is overwritten.")
        # Created readable by the owner only, before any password exists.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "full_name", "temporary_password"])
            for user, password in issue(users):
                writer.writerow([user.email, user.full_name, password])
        self.stdout.write(self.style.SUCCESS(
            f"{len(users)} temporary password(s) written to {path}. Hand each one over, then delete the file."
        ))

    def _users(self, options):
        User = get_user_model()
        if not options["without_password"] and not options["email"]:
            raise CommandError("Name the users: --without-password and/or --email.")
        chosen = {}
        if options["without_password"]:
            for user in User.objects.filter(is_active=True).order_by("email"):
                if not user.has_usable_password():
                    chosen[user.pk] = user
        for email in options["email"]:
            user = User.objects.filter(email__iexact=email.strip()).first()
            if user is None:
                raise CommandError(f"No user with the email {email}.")
            if user.has_usable_password() and not options["reset_existing"]:
                self.stdout.write(self.style.WARNING(
                    f"  skipped {user.email}: already has a password (pass --reset-existing to replace it)"
                ))
                continue
            chosen[user.pk] = user
        return list(chosen.values())
