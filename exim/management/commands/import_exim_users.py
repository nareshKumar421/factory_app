"""
Bring EXIM's logins, and the access they hold there, into this project.

Usage:
    python manage.py import_exim_users                 # dry run: says what it would do
    python manage.py import_exim_users --commit
    python manage.py import_exim_users --commit --add-companies

It reads EXIM's database through the read-only ``exim`` alias (EXIM_DB_NAME and
friends in config/settings.py) and never writes to it. What it does with each
account is set out in ``exim.user_import``; in short:

 - a new login is created with the password the person already uses in EXIM,
   and joins Oil, Mart and Beverages (``--companies`` to change that);
 - an existing login with the same email keeps its password, name and companies;
 - EXIM rights become the same ``exim.*`` rights, EXIM groups become
   "EXIM — <name>" groups, and an EXIM superuser gets "EXIM — All access"
   (never superuser here).

It is a DRY RUN unless ``--commit`` is given, and safe to run again: each run
brings the ``exim.*`` access of every linked login back in line with EXIM.

RUN ``manage.py migrate exim`` FIRST. The EXIM rights are created after that
migration, and the import refuses to run without all of them.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.user_import import (
    DEFAULT_COMPANIES,
    ImportProblem,
    import_accounts,
    read_exim,
    resolve_companies,
)


class Command(BaseCommand):
    help = "Bring EXIM's logins and their access across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Write the changes. Without it nothing is saved.",
        )
        parser.add_argument(
            "--companies", default=",".join(DEFAULT_COMPANIES),
            help="Comma-separated company codes a created login joins, the first "
                 f"as its default (default: {','.join(DEFAULT_COMPANIES)}).",
        )
        parser.add_argument(
            "--add-companies", action="store_true",
            help="Also put logins that already existed here into those companies. "
                 "Off by default: it opens those companies in every module the "
                 "person has rights in, not only EXIM.",
        )
        parser.add_argument(
            "--database", default="exim",
            help="The database alias EXIM is read through (default: exim).",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in settings.DATABASES:
            raise CommandError(
                f"There is no {alias!r} database configured. Set EXIM_DB_NAME, "
                "EXIM_DB_HOST, EXIM_DB_USER and EXIM_DB_PASSWORD (see "
                "config/settings.py). The account needs SELECT on users, "
                "users_groups, users_user_permissions, auth_group, "
                "auth_group_permissions, auth_permission and django_content_type."
            )
        codes = [c.strip() for c in options["companies"].split(",") if c.strip()]
        if not codes:
            raise CommandError("--companies needs at least one company code.")

        commit = options["commit"]
        if not commit:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written\n"))

        try:
            with connections[alias].cursor() as cursor:
                snapshot = read_exim(cursor)
        except DatabaseError as exc:
            raise CommandError(f"Could not read EXIM's users through {alias!r}: {exc}") from exc

        try:
            with transaction.atomic():
                report = import_accounts(
                    snapshot,
                    companies=resolve_companies(codes),
                    add_companies=options["add_companies"],
                )
                if not commit:
                    transaction.set_rollback(True)
        except ImportProblem as exc:
            raise CommandError(str(exc)) from exc

        self._print(report, codes)
        if not commit:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))

    def _print(self, report, codes):
        styles = {
            "create": self.style.SUCCESS,
            "match": self.style.MIGRATE_HEADING,
            "linked": lambda s: s,
            "conflict": self.style.ERROR,
        }
        for a in report.accounts:
            parts = []
            if a.rights_added or a.rights_removed:
                parts.append(f"rights +{a.rights_added} -{a.rights_removed}")
            parts += [f"+group {g}" for g in a.groups_added]
            parts += [f"-group {g}" for g in a.groups_removed]
            if a.companies_added:
                parts.append("companies " + " ".join(a.companies_added))
            if a.companies_missing:
                parts.append(f"not in {', '.join(a.companies_missing)} (--add-companies)")
            parts += a.notes
            if not parts:
                parts.append("no change")
            target = f"→ user {a.user_id}" if a.user_id else ""
            line = f"  {a.action:<8} #{a.exim_id:<4} {a.email:<34} {target:<12} {'; '.join(parts)}"
            self.stdout.write(styles[a.action](line))

        self.stdout.write("")
        self.stdout.write(
            f"EXIM accounts: {len(report.accounts)} - "
            f"{report.count('create')} created, {report.count('match')} matched to a login "
            f"that already existed, {report.count('linked')} already linked, "
            f"{report.count('conflict')} conflict(s)"
        )
        self.stdout.write(f"New logins join: {', '.join(codes)} (default {codes[0]})")
        for g in report.groups:
            self.stdout.write(f"  group {g.name}: {g.rights} right(s){' (created)' if g.created else ''}")
        if report.skipped:
            self.stdout.write(
                self.style.WARNING("Not carried over (not an EXIM module right; stays in EXIM):")
            )
            for code, n in sorted(report.skipped.items()):
                self.stdout.write(f"  {code} x{n}")
