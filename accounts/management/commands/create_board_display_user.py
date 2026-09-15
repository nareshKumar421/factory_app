"""
Create the login a wall screen runs the board carousel under.

Usage:
    python manage.py create_board_display_user --email wall.board@jivo.in \
        --name "Board Carousel Screen" --company JIVO_OIL --dry-run
    python manage.py create_board_display_user --email wall.board@jivo.in \
        --name "Board Carousel Screen" --company JIVO_OIL
    python manage.py create_board_display_user --email wall.board@jivo.in --show

WHAT THIS IS FOR
A TV on a factory wall showing ``/dashboards/carousel`` has to be signed in as
somebody, and that somebody should not be a person. This creates an account that
holds one group — "Dashboards — Control Carousel" — and nothing else: no staff
flag, no superuser, no write right anywhere in the product.

READ THIS BEFORE RUNNING IT ON LIVE
A screen on a wall is a public screen, and this login is what makes it one. The
group it grants is the union of the three boards the carousel rotates, so it
carries everything those boards show — including the factory's wage and power
bill on the Admin board's cost tile. That disclosure is recorded in
``admin_board/permissions.py`` and the business accepted it there; it is
restated here because granting it to a SCREEN is a different decision from
granting it to a person, and it is the screen's location, not the account, that
decides who reads it.

It also cannot be narrowed to "the carousel only". The three boards are gated on
these same rights, so this login can open each of them at its own address too,
and the reports behind them. Closing that would mean minting a right and
teaching every one of those APIs to accept it. So: a display login is a VIEW
login, it is never shared with a person, and its password does not go in a
group chat.

WHAT IT WILL NOT DO
 - It never makes a user staff or superuser.
 - It never grants a right directly; access comes from the group, so revoking
   is one removal rather than an audit.
 - It never silently changes an existing account's password. Re-running against
   an email that exists reconciles the group and the company and leaves the
   password alone unless ``--reset-password`` is given, so the command is safe
   to run twice.
 - It writes nothing at all under ``--dry-run``.
"""

import secrets
import string

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company, UserCompany, UserRole

User = get_user_model()

# The group ``setup_dashboard_groups`` creates for the carousel. Named rather
# than derived so that this command fails loudly if that one has not been run,
# instead of quietly creating a login that can see nothing.
CAROUSEL_GROUP = "Dashboards — Control Carousel"

# What the account is for, on the company row. A display screen is not an
# operator, and the role a UserCompany needs should say so rather than borrowing
# "Admin" and reading, in an audit, as an administrator on a wall.
DISPLAY_ROLE = "Display"

PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 20) -> str:
    """A password nobody has to remember.

    Long and random because it is typed once, into a browser that then keeps the
    session; there is no human on the other side of it to be inconvenienced.
    Letters and digits only — the screen it is typed on is often a TV remote or
    an on-screen keyboard, where punctuation is several menus deep.
    """
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "Create the display login a wall screen runs the board carousel under."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Login email for the screen.")
        parser.add_argument(
            "--name",
            default="Board Carousel Screen",
            help="Full name shown in the product. Say where the screen is.",
        )
        parser.add_argument(
            "--company",
            help=(
                "Company CODE the screen signs into. Decides which plant the "
                "Logistics board reports on. Required when creating."
            ),
        )
        parser.add_argument(
            "--password",
            help="Set this password instead of generating one.",
        )
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Also set a new password on an account that already exists.",
        )
        parser.add_argument(
            "--show",
            action="store_true",
            help="Report what this account holds right now and write nothing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()

        if options["show"]:
            return self._show(email)

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written\n"))

        group = Group.objects.filter(name=CAROUSEL_GROUP).first()
        if group is None:
            raise CommandError(
                f'The group "{CAROUSEL_GROUP}" does not exist. Run '
                "`manage.py setup_dashboard_groups` first — without it this "
                "would create a login that can see nothing."
            )

        existing = User.objects.filter(email__iexact=email).first()
        company = self._resolve_company(options.get("company"), creating=existing is None)

        # Generated even on a dry run, so the operator sees the shape of what
        # they would get; the dry run rolls back before it is ever stored.
        password = options.get("password") or generate_password()
        set_password = existing is None or options["reset_password"]

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": options["name"],
                    # Neither flag, ever. A screen is not staff and is certainly
                    # not a superuser — a superuser bypasses every permission
                    # check in Django, which would make the group below
                    # decorative and the wall able to read the whole product.
                    "is_staff": False,
                    "is_superuser": False,
                },
            )

            if not created:
                self.stdout.write(
                    self.style.WARNING(f"  exists     {email} — reconciling, not recreating")
                )
                if user.is_superuser or user.is_staff:
                    raise CommandError(
                        f"{email} is a staff or superuser account. Refusing to turn a "
                        "privileged login into a wall display — pick a fresh address."
                    )
            else:
                self.stdout.write(self.style.SUCCESS(f"  create     {email}"))

            if set_password:
                user.set_password(password)
                user.save(update_fields=["password"] if not created else None)
                self.stdout.write(self.style.SUCCESS("  password   set"))
            else:
                self.stdout.write("  password   left alone (pass --reset-password to change it)")

            # The group is SET, not added to: this account's whole purpose is
            # the carousel, so a right that arrived some other way is a mistake
            # to be corrected rather than a grant to be preserved. Name anything
            # that goes rather than dropping it silently.
            #
            # EXPECT "Issue Reporter" HERE, EVEN ON A BRAND NEW ACCOUNT.
            # ``issues.signals`` attaches that group to every user the moment it
            # is created, so it is always the first thing this line removes. That
            # is intended, not collateral: Issue Reporter can CREATE issues, and
            # an unattended login on a wall that anybody can walk up to must hold
            # nothing that writes. A person who spots a problem on the screen
            # reports it from their own account.
            dropped = sorted(
                name for name in user.groups.values_list("name", flat=True) if name != CAROUSEL_GROUP
            )
            for name in dropped:
                self.stdout.write(self.style.ERROR(f"      removes group {name}"))
            user.groups.set([group])

            direct = user.user_permissions.count()
            if direct:
                self.stdout.write(
                    self.style.ERROR(f"      removes {direct} directly-held permission(s)")
                )
                user.user_permissions.clear()

            if company is not None:
                self._attach_company(user, company)

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing was written."))
            return

        self.stdout.write(self.style.SUCCESS(f"Display login ready: {email}"))
        self.stdout.write(f"  group    {CAROUSEL_GROUP} ({group.permissions.count()} permission(s))")
        if company is not None:
            self.stdout.write(f"  company  {company.code} — {company.name}")
        if set_password:
            self.stdout.write(self.style.SUCCESS(f"  password {password}"))
            self.stdout.write(
                "  This is printed once and is not recoverable. Put it where the "
                "screen is set up from, not in a chat."
            )
        self.stdout.write(
            "\n  Point the screen at /dashboards/carousel, press F for fullscreen, "
            "and leave it."
        )

    # ---------------------------------------------------------------- helpers #
    def _resolve_company(self, code, *, creating):
        """The company row the screen signs into, or None to leave it alone."""
        if not code:
            if creating:
                raise CommandError(
                    "--company is required when creating: without a company row the "
                    "account cannot sign in, and the company decides which plant the "
                    "Logistics board reports on."
                )
            return None

        company = Company.objects.filter(code__iexact=code).first()
        if company is None:
            known = ", ".join(Company.objects.values_list("code", flat=True).order_by("code"))
            raise CommandError(f"No company with code {code!r}. Known codes: {known}")
        if not company.is_active:
            raise CommandError(f"Company {company.code} is not active.")
        return company

    def _attach_company(self, user, company):
        """Put the screen in one company, as its default, with the display role."""
        role, _ = UserRole.objects.get_or_create(
            name=DISPLAY_ROLE,
            defaults={"description": "Unattended wall screen. Reads boards, does nothing."},
        )
        link, created = UserCompany.objects.get_or_create(
            user=user,
            company=company,
            defaults={"role": role, "is_default": True, "is_active": True},
        )
        if not created:
            link.role = role
            link.is_default = True
            link.is_active = True
            link.save(update_fields=["role", "is_default", "is_active"])
        # One company only. A wall shows one plant; a second company row would
        # give the screen a switcher nobody is there to use, and the Logistics
        # board follows that switcher.
        UserCompany.objects.filter(user=user).exclude(pk=link.pk).delete()
        self.stdout.write(
            self.style.SUCCESS(f"  company    {company.code} ({'added' if created else 'updated'})")
        )

    def _show(self, email):
        """What this account holds right now. Reads only."""
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            self.stdout.write(self.style.WARNING(f"{email}: no such account"))
            return

        self.stdout.write(self.style.SUCCESS(f"{user.email} — {user.full_name}"))
        self.stdout.write(
            f"  active {user.is_active} · staff {user.is_staff} · superuser {user.is_superuser}"
        )
        groups = sorted(user.groups.values_list("name", flat=True))
        self.stdout.write(f"  groups: {', '.join(groups) if groups else '(none)'}")

        direct = sorted(
            f"{p.content_type.app_label}.{p.codename}"
            for p in user.user_permissions.select_related("content_type")
        )
        if direct:
            self.stdout.write(self.style.WARNING(f"  {len(direct)} permission(s) held directly:"))
            for code in direct:
                self.stdout.write(f"    - {code}")

        for link in UserCompany.objects.filter(user=user).select_related("company", "role"):
            default = " (default)" if link.is_default else ""
            self.stdout.write(f"  company: {link.company.code} as {link.role.name}{default}")
