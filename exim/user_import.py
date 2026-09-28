"""Bring EXIM's logins, and the access they hold, into this project.

The first step of moving EXIM (import/export) across: its users arrive before
any of its screens, so that each module that follows lands in front of people
who can already sign in and already hold the right to see it.

WHAT IT DOES FOR EACH EXIM ACCOUNT
 - Finds its login here: the one it was linked to on an earlier run, else the
   one with the same email (case-insensitive), else it creates one.
 - A CREATED login gets EXIM's stored password hash as it is, so the person
   signs in with the password they already use. Both projects hash with
   Django's default PBKDF2, and the hash records its own iteration count, so it
   verifies here unchanged and is upgraded on first sign-in. A MATCHED login
   keeps its own password and name: somebody already signing in here is not
   made to switch to their EXIM password.
 - Mirrors its EXIM access, and only that: directly held EXIM rights become the
   same ``exim.*`` rights held directly, and each EXIM group it is in becomes
   the group "EXIM — <name>" (see ``exim.access`` for the mapping). A right is
   added or removed to match EXIM; nothing outside ``exim.*`` and the "EXIM — "
   groups is ever touched.
 - An EXIM superuser goes into "EXIM — All access", which holds every EXIM
   right. It is NOT made a superuser here: that would open gate, dispatch, cash
   book and everything else in this project.
 - A created login joins every company given (Oil, Mart and Beverages by
   default), the first as its default, because EXIM has no companies and its
   users see all three today. A MATCHED login's companies are left alone unless
   asked (``add_companies``): company membership opens a company in every
   module a person has rights in, not only this one, so widening it for somebody
   who already works here is a decision about them, not about EXIM.
 - An inactive EXIM account keeps no EXIM access. A login the import created
   follows EXIM's active flag; a matched one never does.

It is meant to be re-run until the last module is cut over, because EXIM stays
the place access is granted until then. After that, stop running it: it would
undo any ``exim.*`` grant made here since.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.contrib.auth.models import Group, Permission

from company.models import Company, UserCompany, UserRole

from .access import BY_SOURCE
from .models import EximUser

User = get_user_model()

GROUP_PREFIX = "EXIM — "
ALL_ACCESS_GROUP = f"{GROUP_PREFIX}All access"
ROLE_NAME = "Import / Export"
DEFAULT_COMPANIES = ("JIVO_OIL", "JIVO_MART", "JIVO_BEVERAGES")

# EXIM's user table is ``users`` (its User sets db_table), so Django named the
# two many-to-many tables after it.
GROUPS_SQL = "SELECT id, name FROM auth_group"
GROUP_RIGHTS_SQL = """
    SELECT gp.group_id, ct.app_label, p.codename
      FROM auth_group_permissions gp
      JOIN auth_permission p ON p.id = gp.permission_id
      JOIN django_content_type ct ON ct.id = p.content_type_id
"""
MEMBERSHIP_SQL = "SELECT user_id, group_id FROM users_groups"
DIRECT_RIGHTS_SQL = """
    SELECT up.user_id, ct.app_label, p.codename
      FROM users_user_permissions up
      JOIN auth_permission p ON p.id = up.permission_id
      JOIN django_content_type ct ON ct.id = p.content_type_id
"""
USERS_SQL = "SELECT id, email, name, password, is_active, is_superuser FROM users ORDER BY id"


class ImportProblem(Exception):
    """The import cannot run as things stand; the message says what to fix."""


@dataclass(frozen=True)
class EximAccount:
    id: int
    email: str
    name: str
    password: str
    is_active: bool
    is_superuser: bool
    #: Names of the EXIM groups it is in.
    groups: frozenset = frozenset()
    #: Directly held rights, as (EXIM app label, codename).
    rights: frozenset = frozenset()


@dataclass(frozen=True)
class EximSnapshot:
    accounts: list
    #: EXIM group name -> its rights, as (EXIM app label, codename).
    groups: dict


def read_exim(cursor) -> EximSnapshot:
    """Read EXIM's accounts, groups and grants. SELECTs only."""
    cursor.execute(GROUPS_SQL)
    group_names = {gid: name for gid, name in cursor.fetchall()}

    group_rights = defaultdict(set)
    cursor.execute(GROUP_RIGHTS_SQL)
    for gid, app_label, codename in cursor.fetchall():
        group_rights[gid].add((app_label, codename))

    memberships = defaultdict(set)
    cursor.execute(MEMBERSHIP_SQL)
    for uid, gid in cursor.fetchall():
        memberships[uid].add(group_names[gid])

    direct = defaultdict(set)
    cursor.execute(DIRECT_RIGHTS_SQL)
    for uid, app_label, codename in cursor.fetchall():
        direct[uid].add((app_label, codename))

    cursor.execute(USERS_SQL)
    accounts = [
        EximAccount(
            id=uid,
            email=(email or "").strip(),
            name=(name or "").strip(),
            password=password or "",
            is_active=bool(is_active),
            is_superuser=bool(is_superuser),
            groups=frozenset(memberships[uid]),
            rights=frozenset(direct[uid]),
        )
        for uid, email, name, password, is_active, is_superuser in cursor.fetchall()
    ]
    groups = {name: frozenset(group_rights[gid]) for gid, name in group_names.items()}
    return EximSnapshot(accounts=accounts, groups=groups)


@dataclass
class AccountResult:
    exim_id: int
    email: str
    #: create | match | linked | conflict
    action: str = ""
    user_id: int | None = None
    rights_added: int = 0
    rights_removed: int = 0
    groups_added: list = field(default_factory=list)
    groups_removed: list = field(default_factory=list)
    companies_added: list = field(default_factory=list)
    #: Companies given to the import that this login is not in, and was not put in.
    companies_missing: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.action == "create" or self.rights_added or self.rights_removed
            or self.groups_added or self.groups_removed or self.companies_added
        )


@dataclass
class GroupResult:
    name: str
    created: bool
    rights: int


@dataclass
class Report:
    accounts: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    #: "app.codename" -> how many accounts and groups held it. Rights that are
    #: not EXIM module rights (its user management, Django admin) stay behind.
    skipped: Counter = field(default_factory=Counter)

    def count(self, action: str) -> int:
        return sum(1 for a in self.accounts if a.action == action)


def resolve_companies(codes) -> list:
    """The companies a created login joins, in the order given."""
    companies = []
    for code in codes:
        company = Company.objects.filter(code__iexact=code).first()
        if company is None:
            known = ", ".join(Company.objects.values_list("code", flat=True).order_by("code"))
            raise ImportProblem(f"No company with code {code!r}. Known codes: {known}")
        if not company.is_active:
            raise ImportProblem(f"Company {company.code} is not active.")
        companies.append(company)
    return companies


def catalogue_rights() -> dict:
    """codename -> Permission for every EXIM right, or ImportProblem if any is missing."""
    found = {
        p.codename: p
        for p in Permission.objects.filter(
            content_type__app_label="exim", codename__in=BY_SOURCE.values(),
        )
    }
    missing = sorted(set(BY_SOURCE.values()) - set(found))
    if missing:
        raise ImportProblem(
            f"{len(missing)} of the {len(BY_SOURCE)} EXIM rights do not exist here yet "
            f"(first: exim.{missing[0]}). Run `manage.py migrate exim`: Django creates "
            "them once the migration has run."
        )
    return found


def import_accounts(snapshot: EximSnapshot, *, companies, add_companies=False) -> Report:
    """Sync every EXIM account into this project. Writes; wrap it in a transaction."""
    rights = catalogue_rights()
    report = Report()
    groups, all_access = _sync_groups(snapshot, rights, report)
    role, _ = UserRole.objects.get_or_create(
        name=ROLE_NAME, defaults={"description": "Joined from EXIM by import_exim_users."},
    )

    # Two EXIM accounts whose addresses differ only in case would both land on
    # one login here, and each run would flip it between their two sets of
    # access. They are merged in EXIM, not guessed at here.
    by_address = Counter(a.email.lower() for a in snapshot.accounts)

    for account in snapshot.accounts:
        result = AccountResult(exim_id=account.id, email=account.email)
        report.accounts.append(result)
        if by_address[account.email.lower()] > 1:
            result.action = "conflict"
            result.notes.append("another EXIM account has this address too; merge them in EXIM first")
            continue
        _sync_account(
            account, result, rights=rights, groups=groups, all_access=all_access,
            companies=companies, role=role, add_companies=add_companies, report=report,
        )
    return report


def _map(held, report: Report) -> set:
    """EXIM (app label, codename) pairs -> codenames here, counting what stays behind."""
    mapped = set()
    for pair in held:
        codename = BY_SOURCE.get(pair)
        if codename is None:
            report.skipped[".".join(pair)] += 1
        else:
            mapped.add(codename)
    return mapped


def _sync_groups(snapshot: EximSnapshot, rights: dict, report: Report):
    """Make "EXIM — <name>" hold what the EXIM group holds.

    Returns ({EXIM group name: Group}, the "EXIM — All access" Group).
    """
    wanted = {ALL_ACCESS_GROUP: set(rights)}
    by_exim_name = {}
    for name, held in sorted(snapshot.groups.items()):
        local = f"{GROUP_PREFIX}{name}"
        if len(local) > Group._meta.get_field("name").max_length:
            raise ImportProblem(f"EXIM group {name!r} is too long to name here as {local!r}.")
        if local == ALL_ACCESS_GROUP:
            raise ImportProblem(
                f"EXIM has a group called {name!r}, which would land on {ALL_ACCESS_GROUP!r}, "
                "the group EXIM superusers get. Rename it in EXIM first."
            )
        wanted[local] = _map(held, report)
        by_exim_name[name] = local

    made = {}
    for local, codenames in wanted.items():
        group, created = Group.objects.get_or_create(name=local)
        group.permissions.set([rights[c] for c in codenames])
        made[local] = group
        report.groups.append(GroupResult(local, created, len(codenames)))

    return {name: made[local] for name, local in by_exim_name.items()}, made[ALL_ACCESS_GROUP]


def _sync_account(account, result, *, rights, groups, all_access, companies, role, add_companies, report):
    link = EximUser.objects.select_related("user").filter(exim_id=account.id).first()
    if link is not None:
        user = link.user
        result.action = "linked"
    else:
        matches = list(User.objects.filter(email__iexact=account.email))
        if len(matches) > 1:
            result.action = "conflict"
            result.notes.append(
                f"{len(matches)} logins here answer to this address; merge them first"
            )
            return
        if matches:
            user = matches[0]
            result.action = "match"
        else:
            user = _create_user(account, result)
            result.action = "create"
        link = EximUser(exim_id=account.id, user=user, created_user=result.action == "create")
    link.exim_email = account.email
    link.save()
    result.user_id = user.pk

    if link.created_user and user.is_active != account.is_active:
        user.is_active = account.is_active
        user.save(update_fields=["is_active"])
        result.notes.append("activated, as in EXIM" if account.is_active else "deactivated, as in EXIM")
    if not link.created_user and account.is_active and not user.is_active:
        result.notes.append("this login is inactive here, so the access it now holds is unused")
    if not account.is_active:
        result.notes.append("inactive in EXIM: holds no EXIM access")

    # Direct rights.
    wanted = {rights[c] for c in _map(account.rights, report)} if account.is_active else set()
    held = set(user.user_permissions.filter(content_type__app_label="exim"))
    if wanted - held:
        user.user_permissions.add(*(wanted - held))
    if held - wanted:
        user.user_permissions.remove(*(held - wanted))
    result.rights_added, result.rights_removed = len(wanted - held), len(held - wanted)

    # Groups.
    wanted_groups = set()
    if account.is_active:
        wanted_groups = {groups[name] for name in account.groups if name in groups}
        if account.is_superuser:
            wanted_groups.add(all_access)
    held_groups = set(user.groups.filter(name__startswith=GROUP_PREFIX))
    if wanted_groups - held_groups:
        user.groups.add(*(wanted_groups - held_groups))
    if held_groups - wanted_groups:
        user.groups.remove(*(held_groups - wanted_groups))
    result.groups_added = sorted(g.name for g in wanted_groups - held_groups)
    result.groups_removed = sorted(g.name for g in held_groups - wanted_groups)

    # Companies.
    member_of = set(UserCompany.objects.filter(user=user).values_list("company_id", flat=True))
    missing = [c for c in companies if c.pk not in member_of]
    if missing and (result.action == "create" or add_companies):
        has_default = UserCompany.objects.filter(user=user, is_default=True).exists()
        for company in missing:
            UserCompany.objects.create(
                user=user, company=company, role=role,
                is_default=not has_default, is_active=True,
            )
            has_default = True
            result.companies_added.append(company.code)
    else:
        result.companies_missing = [c.code for c in missing]


def _create_user(account: EximAccount, result: AccountResult):
    name_field = User._meta.get_field("full_name")
    name = account.name or account.email.split("@")[0]
    if len(name) > name_field.max_length:
        result.notes.append(f"name cut to {name_field.max_length} characters")
        name = name[: name_field.max_length]

    user = User(
        email=User.objects.normalize_email(account.email),
        full_name=name,
        is_active=account.is_active,
        is_staff=False,
        is_superuser=False,
    )
    # The hash is copied as it is, never re-hashed: set_password would hash the
    # hash. A hash this project cannot verify is replaced by an unusable one, so
    # the login exists (EXIM's records point at it) but needs a password reset.
    if not is_password_usable(account.password):
        user.set_unusable_password()
        result.notes.append("no usable password in EXIM; needs a password set here")
    else:
        try:
            identify_hasher(account.password)
        except ValueError:
            user.set_unusable_password()
            result.notes.append("EXIM's password uses a hasher this project lacks; needs a reset")
        else:
            user.password = account.password
    user.save()
    return user
