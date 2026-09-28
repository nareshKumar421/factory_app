"""
EXIM's logins arriving here.

What is worth testing is the set of promises ``import_exim_users`` makes about
LIVE logins, most of which exist to stop it widening anybody's access:

  - a created login signs in with the password the person already uses in EXIM;
  - a login that already existed keeps its password, its name and its companies;
  - EXIM access arrives as ``exim.*`` rights and "EXIM — " groups and nothing
    else, and an EXIM superuser is never made a superuser here;
  - a re-run brings that access back in line with EXIM without touching any
    right or group from outside EXIM;
  - ``--commit`` is the only way anything is written.
"""

import sqlite3
from io import StringIO
from unittest import mock

from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.contrib.auth.models import Group, Permission
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole

from .access import BY_SOURCE, CATALOGUE
from .models import EximUser
from .user_import import (
    ALL_ACCESS_GROUP,
    DEFAULT_COMPANIES,
    EximAccount,
    EximSnapshot,
    ImportProblem,
    import_accounts,
    read_exim,
    resolve_companies,
)

EXIM_PASSWORD = "exim-secret-42"


def exim_hash(password=EXIM_PASSWORD):
    """A PBKDF2 hash at an iteration count this project would not choose itself,
    as EXIM's older Django writes them."""
    return PBKDF2PasswordHasher().encode(password, "eximsalt123", iterations=1000)


def account(id=1, email="ramesh@jivo.in", **kw):
    kw.setdefault("name", "Ramesh Kumar")
    kw.setdefault("password", exim_hash())
    kw.setdefault("is_active", True)
    kw.setdefault("is_superuser", False)
    kw["groups"] = frozenset(kw.get("groups", ()))
    kw["rights"] = frozenset(kw.get("rights", ()))
    return EximAccount(id=id, email=email, **kw)


def snapshot(*accounts, groups=None):
    return EximSnapshot(
        accounts=list(accounts),
        groups={name: frozenset(rights) for name, rights in (groups or {}).items()},
    )


def fresh(user):
    """Re-read, so has_perm does not answer from its cache."""
    return User.objects.get(pk=user.pk)


class CatalogueTests(TestCase):
    def test_every_exim_right_exists_under_the_exim_label(self):
        here = set(
            Permission.objects.filter(content_type__app_label="exim").values_list("codename", flat=True)
        )
        self.assertEqual(here, {codename for _app, codename, _name in CATALOGUE})
        self.assertEqual(len(here), 164)

    def test_codenames_are_unique_so_one_label_can_hold_them(self):
        self.assertEqual(len(BY_SOURCE), len(CATALOGUE))
        self.assertEqual(len(set(BY_SOURCE.values())), len(CATALOGUE))

    def test_exim_user_management_is_not_carried(self):
        """Who may manage logins is this project's question, not EXIM's."""
        codenames = set(BY_SOURCE.values())
        for code in ("add_user", "change_user", "delete_user", "view_user"):
            self.assertNotIn(code, codenames)


class ReadEximTests(SimpleTestCase):
    """The SQL against EXIM's real table names, on a stand-in database."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT, password TEXT,
                                is_active BOOL, is_superuser BOOL, is_staff BOOL, last_login TEXT);
            CREATE TABLE auth_group (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE django_content_type (id INTEGER PRIMARY KEY, app_label TEXT, model TEXT);
            CREATE TABLE auth_permission (id INTEGER PRIMARY KEY, content_type_id INT, codename TEXT, name TEXT);
            CREATE TABLE auth_group_permissions (id INTEGER PRIMARY KEY, group_id INT, permission_id INT);
            CREATE TABLE users_groups (id INTEGER PRIMARY KEY, user_id INT, group_id INT);
            CREATE TABLE users_user_permissions (id INTEGER PRIMARY KEY, user_id INT, permission_id INT);

            INSERT INTO users VALUES (1, ' ramesh@jivo.in ', 'Ramesh', 'pbkdf2_sha256$x', 1, 0, 0, NULL),
                                     (2, 'boss@jivo.in', 'Boss', 'pbkdf2_sha256$y', 1, 1, 1, NULL),
                                     (3, 'gone@jivo.in', 'Gone', 'pbkdf2_sha256$z', 0, 0, 0, NULL);
            INSERT INTO auth_group VALUES (10, 'Stock team'), (11, 'Empty');
            INSERT INTO django_content_type VALUES (100, 'tank', 'tankdata'), (101, 'stock', 'stockstatus');
            INSERT INTO auth_permission VALUES (500, 100, 'view_tankdata', 'x'),
                                               (501, 101, 'change_stockstatus', 'x');
            INSERT INTO auth_group_permissions VALUES (1, 10, 501);
            INSERT INTO users_groups VALUES (1, 1, 10);
            INSERT INTO users_user_permissions VALUES (1, 1, 500);
            """
        )

    def test_reads_accounts_with_their_groups_and_direct_rights(self):
        snap = read_exim(self.db.cursor())
        by_id = {a.id: a for a in snap.accounts}
        self.assertEqual(sorted(by_id), [1, 2, 3])

        ramesh = by_id[1]
        self.assertEqual(ramesh.email, "ramesh@jivo.in")
        self.assertEqual(ramesh.groups, {"Stock team"})
        self.assertEqual(ramesh.rights, {("tank", "view_tankdata")})
        self.assertTrue(by_id[2].is_superuser)
        self.assertFalse(by_id[3].is_active)

        self.assertEqual(snap.groups["Stock team"], {("stock", "change_stockstatus")})
        self.assertEqual(snap.groups["Empty"], frozenset())


class ImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")

    def run_import(self, snap, **kw):
        return import_accounts(snap, companies=resolve_companies(DEFAULT_COMPANIES), **kw)

    def existing(self, email="Ramesh@Jivo.in", companies=("JIVO_OIL",), **kw):
        user = User.objects.create_user(
            email=email, password="factory-pass", full_name=kw.pop("full_name", "R. Kumar (plant)"), **kw
        )
        role, _ = UserRole.objects.get_or_create(name="Gate")
        for i, code in enumerate(companies):
            UserCompany.objects.create(
                user=user, company=Company.objects.get(code=code), role=role, is_default=i == 0,
            )
        return user

    # -- a new login ------------------------------------------------------ #

    def test_a_new_login_signs_in_with_its_exim_password(self):
        report = self.run_import(snapshot(account()))

        user = User.objects.get(email="ramesh@jivo.in")
        self.assertEqual(report.accounts[0].action, "create")
        self.assertTrue(user.check_password(EXIM_PASSWORD))
        self.assertEqual(user.full_name, "Ramesh Kumar")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        link = EximUser.objects.get(exim_id=1)
        self.assertEqual(link.user, user)
        self.assertTrue(link.created_user)

    def test_a_new_login_joins_all_three_companies_oil_first(self):
        self.run_import(snapshot(account()))

        links = UserCompany.objects.filter(user__email="ramesh@jivo.in")
        self.assertEqual(
            {l.company.code for l in links}, {"JIVO_OIL", "JIVO_MART", "JIVO_BEVERAGES"},
        )
        self.assertEqual([l.company.code for l in links if l.is_default], ["JIVO_OIL"])
        self.assertEqual({l.role.name for l in links}, {"Import / Export"})

    def test_a_hash_this_project_cannot_verify_leaves_the_login_needing_a_reset(self):
        report = self.run_import(snapshot(account(password="bogus$1$abc")))

        user = User.objects.get(email="ramesh@jivo.in")
        self.assertFalse(user.has_usable_password())
        self.assertIn("needs a reset", " ".join(report.accounts[0].notes))

    # -- a login that already existed ------------------------------------ #

    def test_an_existing_login_keeps_its_password_name_and_companies(self):
        user = self.existing()

        report = self.run_import(snapshot(account(rights={("tank", "view_tankdata")})))

        result = report.accounts[0]
        self.assertEqual(result.action, "match")
        self.assertEqual(result.user_id, user.pk)
        user = fresh(user)
        self.assertTrue(user.check_password("factory-pass"))
        self.assertFalse(user.check_password(EXIM_PASSWORD))
        self.assertEqual(user.full_name, "R. Kumar (plant)")
        self.assertEqual(
            list(UserCompany.objects.filter(user=user).values_list("company__code", flat=True)),
            ["JIVO_OIL"],
        )
        self.assertEqual(result.companies_missing, ["JIVO_MART", "JIVO_BEVERAGES"])
        self.assertTrue(user.has_perm("exim.view_tankdata"))
        self.assertFalse(EximUser.objects.get(exim_id=1).created_user)

    def test_add_companies_widens_an_existing_login_keeping_its_default(self):
        user = self.existing()

        self.run_import(snapshot(account()), add_companies=True)

        links = UserCompany.objects.filter(user=user)
        self.assertEqual(links.count(), 3)
        self.assertEqual([l.company.code for l in links if l.is_default], ["JIVO_OIL"])

    # -- access ----------------------------------------------------------- #

    def test_exim_rights_arrive_under_the_exim_label(self):
        self.run_import(snapshot(account(rights={("tank", "view_tankdata"), ("accounts", "view_exim_rates")})))

        user = fresh(User.objects.get(email="ramesh@jivo.in"))
        self.assertTrue(user.has_perm("exim.view_tankdata"))
        self.assertTrue(user.has_perm("exim.view_exim_rates"))
        self.assertFalse(user.has_perm("exim.change_tankdata"))

    def test_exim_groups_arrive_as_exim_prefixed_groups(self):
        snap = snapshot(
            account(groups={"Stock team"}),
            groups={"Stock team": {("stock", "view_stockstatus"), ("stock", "change_stockstatus")}},
        )

        self.run_import(snap)

        group = Group.objects.get(name="EXIM — Stock team")
        self.assertEqual(group.permissions.count(), 2)
        user = fresh(User.objects.get(email="ramesh@jivo.in"))
        self.assertIn(group, user.groups.all())
        self.assertTrue(user.has_perm("exim.change_stockstatus"))
        self.assertEqual(user.user_permissions.count(), 0)

    def test_an_exim_superuser_gets_all_exim_access_and_nothing_else(self):
        self.run_import(snapshot(account(is_superuser=True)))

        user = fresh(User.objects.get(email="ramesh@jivo.in"))
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertIn(ALL_ACCESS_GROUP, user.groups.values_list("name", flat=True))
        self.assertEqual(Group.objects.get(name=ALL_ACCESS_GROUP).permissions.count(), len(CATALOGUE))
        self.assertTrue(user.has_perm("exim.delete_tanklog"))
        self.assertEqual(
            {p for p in user.get_all_permissions() if not p.startswith("exim.")}, set(),
            "an EXIM superuser holds EXIM rights and nothing else",
        )

    def test_rights_that_are_not_exim_module_rights_stay_behind(self):
        report = self.run_import(
            snapshot(account(rights={("accounts", "add_user"), ("admin", "view_logentry"), ("tank", "view_tanklog")}))
        )

        user = fresh(User.objects.get(email="ramesh@jivo.in"))
        self.assertEqual(report.skipped, {"accounts.add_user": 1, "admin.view_logentry": 1})
        self.assertFalse(user.has_perm("accounts.add_user"))
        self.assertEqual(
            list(user.user_permissions.values_list("codename", flat=True)), ["view_tanklog"],
        )

    # -- re-running ------------------------------------------------------- #

    def test_a_rerun_mirrors_exim_and_leaves_everything_else_alone(self):
        user = self.existing()
        gate = Group.objects.create(name="Gate staff")
        user.groups.add(gate)
        own = Permission.objects.get(codename="can_view_tomorrow_run")
        user.user_permissions.add(own)
        groups = {"Stock team": {("stock", "view_stockstatus")}}

        self.run_import(snapshot(
            account(rights={("tank", "view_tankdata"), ("tank", "change_tankdata")}, groups={"Stock team"}),
            groups=groups,
        ))
        report = self.run_import(snapshot(account(rights={("tank", "view_tankdata")}), groups=groups))

        result = report.accounts[0]
        self.assertEqual(result.action, "linked")
        self.assertEqual((result.rights_added, result.rights_removed), (0, 1))
        self.assertEqual(result.groups_removed, ["EXIM — Stock team"])
        user = fresh(user)
        self.assertTrue(user.has_perm("exim.view_tankdata"))
        self.assertFalse(user.has_perm("exim.change_tankdata"))
        self.assertFalse(user.has_perm("exim.view_stockstatus"))
        self.assertTrue(user.has_perm("tomorrow_run.can_view_tomorrow_run"))
        self.assertIn(gate, user.groups.all())
        self.assertEqual(EximUser.objects.filter(exim_id=1).count(), 1)

    def test_a_rerun_does_not_put_a_created_login_back_into_a_company(self):
        """Removing a company by hand is a decision; a re-run respects it."""
        self.run_import(snapshot(account()))
        UserCompany.objects.filter(user__email="ramesh@jivo.in", company=self.bev).delete()

        report = self.run_import(snapshot(account()))

        self.assertFalse(UserCompany.objects.filter(user__email="ramesh@jivo.in", company=self.bev).exists())
        self.assertEqual(report.accounts[0].companies_missing, ["JIVO_BEVERAGES"])

    # -- inactive accounts ------------------------------------------------ #

    def test_an_inactive_exim_account_is_created_inactive_with_no_access(self):
        self.run_import(snapshot(account(is_active=False, is_superuser=True, rights={("tank", "view_tankdata")})))

        user = fresh(User.objects.get(email="ramesh@jivo.in"))
        self.assertFalse(user.is_active)
        self.assertEqual(user.user_permissions.count(), 0)
        self.assertFalse(user.groups.filter(name__startswith="EXIM — ").exists())
        self.assertTrue(EximUser.objects.filter(exim_id=1).exists(), "kept, so EXIM's records can point at it")

    def test_a_created_login_follows_exim_active_flag_but_a_matched_one_never_does(self):
        matched = self.existing(email="boss@jivo.in")
        self.run_import(snapshot(account(), account(id=2, email="boss@jivo.in", rights={("tank", "view_tankdata")})))

        self.run_import(snapshot(
            account(is_active=False),
            account(id=2, email="boss@jivo.in", is_active=False, rights={("tank", "view_tankdata")}),
        ))

        self.assertFalse(User.objects.get(email="ramesh@jivo.in").is_active)
        matched = fresh(matched)
        self.assertTrue(matched.is_active)
        self.assertFalse(matched.has_perm("exim.view_tankdata"))

    # -- conflicts -------------------------------------------------------- #

    def test_two_exim_accounts_differing_only_in_case_are_left_for_a_person(self):
        report = self.run_import(snapshot(account(), account(id=2, email="RAMESH@jivo.in")))

        self.assertEqual([a.action for a in report.accounts], ["conflict", "conflict"])
        self.assertFalse(User.objects.filter(email__iexact="ramesh@jivo.in").exists())

    def test_two_logins_here_for_one_address_are_left_for_a_person(self):
        self.existing(email="ramesh@jivo.in")
        User.objects.create_user(email="RAMESH@jivo.in", password="x", full_name="Other")

        report = self.run_import(snapshot(account()))

        self.assertEqual(report.accounts[0].action, "conflict")
        self.assertFalse(EximUser.objects.exists())

    def test_an_exim_group_that_would_land_on_all_access_is_refused(self):
        """Else it would quietly replace what every EXIM superuser holds."""
        with self.assertRaisesMessage(ImportProblem, "Rename it in EXIM"):
            self.run_import(snapshot(account(), groups={"All access": {("tank", "view_tankdata")}}))

    def test_refuses_to_run_until_every_exim_right_exists(self):
        Permission.objects.filter(content_type__app_label="exim", codename="view_tanklog").delete()

        with self.assertRaisesMessage(ImportProblem, "migrate exim"):
            self.run_import(snapshot(account()))


class CommandTests(TestCase):
    """The command reads through a database alias; here it is pointed at the
    test database and the reader is replaced, since there is no EXIM."""

    @classmethod
    def setUpTestData(cls):
        for code in DEFAULT_COMPANIES:
            Company.objects.create(name=code, code=code)

    def call(self, *args):
        out = StringIO()
        with mock.patch(
            "exim.management.commands.import_exim_users.read_exim",
            return_value=snapshot(account(rights={("tank", "view_tankdata")})),
        ):
            call_command("import_exim_users", "--database", "default", *args, stdout=out)
        return out.getvalue()

    def test_without_commit_nothing_is_written(self):
        users = User.objects.count()

        out = self.call()

        self.assertIn("DRY RUN - nothing was written", out)
        self.assertIn("create", out)
        self.assertEqual(User.objects.count(), users)
        self.assertFalse(EximUser.objects.exists())
        self.assertFalse(Group.objects.filter(name=ALL_ACCESS_GROUP).exists())

    def test_commit_writes(self):
        out = self.call("--commit")

        self.assertNotIn("DRY RUN", out)
        user = User.objects.get(email="ramesh@jivo.in")
        self.assertTrue(fresh(user).has_perm("exim.view_tankdata"))
        self.assertIn("1 created", out)

    def test_no_exim_database_is_an_error_that_says_what_to_set(self):
        with self.assertRaisesMessage(CommandError, "EXIM_DB_NAME"):
            call_command("import_exim_users", "--database", "nowhere", stdout=StringIO())

    def test_an_unknown_company_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "No company with code 'JIVO_NOPE'"):
            self.call("--companies", "JIVO_OIL,JIVO_NOPE")
