"""Per-meter scoping: only the keeper of a meter may change it or read it.

Two halves, and the second is the one that is easy to get wrong:

* the rule itself — an unassigned user is refused, a keeper is allowed on their
  own meters and refused on everybody else's, a superuser is exempt;
* the admin endpoints that configure it, where `my-electricity-meters/` must NOT
  require the admin permission (a screen cannot correctly disable an action it
  is not allowed to ask about) while every write endpoint must.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from maintenance import meter_scope
from maintenance.models import DailyElectricityReading, ElectricityMeter
from maintenance.models_manager import UserElectricityMeter

METERS_URL = "/api/v1/maintenance/electricity-meters/"
READINGS_URL = "/api/v1/maintenance/daily-electricity-readings/"
MINE_URL = "/api/v1/maintenance/my-electricity-meters/"
ASSIGN_URL = "/api/v1/maintenance/user-electricity-meters/"
GAPS_URL = "/api/v1/maintenance/user-electricity-meters/gaps/"


def make_user(email, *codenames, superuser=False):
    User = get_user_model()
    user = User.objects.create_user(
        email=email,
        password="testpass123",
        full_name=email.split("@")[0].title(),
        employee_code=email.split("@")[0].upper(),
    )
    if superuser:
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="maintenance", codename__in=codenames
            )
        )
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class MeterScopeRuleTests(TestCase):
    """The rule, enforced at the endpoints that write."""

    def setUp(self):
        self.boiler = ElectricityMeter.objects.create(
            name="Boiler", rate_per_unit=Decimal("9")
        )
        self.terrace = ElectricityMeter.objects.create(
            name="Terrace", rate_per_unit=Decimal("9")
        )
        # Every right on the register, so nothing below can pass or fail for
        # want of a permission — the assignment is the only variable.
        self.every_right = (
            "can_view_daily_electricity",
            "can_view_electricity_meter",
            "can_manage_electricity_meter",
            "can_add_daily_electricity",
            "can_edit_daily_electricity",
            "can_delete_daily_electricity",
        )
        self.keeper = make_user("keeper@example.com", *self.every_right)
        UserElectricityMeter.objects.create(user=self.keeper, meter=self.boiler)

    # ---- the meter master -------------------------------------------------

    def test_a_keeper_may_change_his_own_meter(self):
        response = client_for(self.keeper).patch(
            f"{METERS_URL}{self.boiler.id}/", {"location": "Block A"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.boiler.refresh_from_db()
        self.assertEqual(self.boiler.location, "Block A")

    def test_a_keeper_may_not_change_somebody_elses_meter(self):
        response = client_for(self.keeper).patch(
            f"{METERS_URL}{self.terrace.id}/",
            {"multiplying_factor": "40"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.terrace.refresh_from_db()
        self.assertEqual(self.terrace.multiplying_factor, Decimal("1"))

    def test_the_refusal_names_both_sides(self):
        """"You can't" with no second half is what makes a 403 feel like a bug."""
        response = client_for(self.keeper).patch(
            f"{METERS_URL}{self.terrace.id}/", {"location": "x"}, format="json"
        )
        detail = str(response.data["detail"])
        self.assertIn("Terrace", detail)
        self.assertIn("Boiler", detail)

    def test_a_keeper_may_not_delete_somebody_elses_meter(self):
        response = client_for(self.keeper).delete(f"{METERS_URL}{self.terrace.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ElectricityMeter.objects.filter(pk=self.terrace.pk).exists())

    def test_an_unassigned_user_is_refused_and_told_where_to_go(self):
        stranger = make_user("stranger@example.com", *self.every_right)
        response = client_for(stranger).patch(
            f"{METERS_URL}{self.boiler.id}/", {"location": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not set as the manager", str(response.data["detail"]))

    def test_a_deactivated_assignment_no_longer_grants_anything(self):
        UserElectricityMeter.objects.filter(user=self.keeper).update(is_active=False)
        response = client_for(self.keeper).patch(
            f"{METERS_URL}{self.boiler.id}/", {"location": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_a_superuser_is_exempt(self):
        """Without this the first deploy locks out whoever would configure it."""
        root = make_user("root@example.com", superuser=True)
        response = client_for(root).patch(
            f"{METERS_URL}{self.terrace.id}/", {"location": "anywhere"}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_creating_a_meter_makes_the_creator_its_keeper(self):
        """A meter born unkept would be uneditable by everyone but a superuser."""
        response = client_for(self.keeper).post(
            METERS_URL, {"name": "Chiller"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            UserElectricityMeter.objects.filter(
                user=self.keeper, meter_id=response.data["id"], is_active=True
            ).exists()
        )
        follow_up = client_for(self.keeper).patch(
            f"{METERS_URL}{response.data['id']}/", {"location": "Roof"}, format="json"
        )
        self.assertEqual(follow_up.status_code, 200)

    # ---- the readings -----------------------------------------------------

    def _post_reading(self, user, meter, day="2026-08-01", closing="40"):
        return client_for(user).post(
            READINGS_URL,
            {
                "meter": meter.id,
                "date": day,
                "opening_reading": "0",
                "closing_reading": closing,
            },
            format="json",
        )

    def test_a_keeper_may_book_a_day_on_his_own_meter(self):
        self.assertEqual(self._post_reading(self.keeper, self.boiler).status_code, 201)

    def test_a_keeper_may_not_book_a_day_on_another_meter(self):
        response = self._post_reading(self.keeper, self.terrace)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DailyElectricityReading.objects.filter(meter=self.terrace).exists())

    def test_a_keeper_may_not_correct_a_reading_on_another_meter(self):
        reading = DailyElectricityReading.objects.create(
            meter=self.terrace,
            date=date(2026, 8, 1),
            opening_reading=Decimal("0"),
            closing_reading=Decimal("10"),
        )
        response = client_for(self.keeper).patch(
            f"{READINGS_URL}{reading.id}/", {"closing_reading": "99"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        reading.refresh_from_db()
        self.assertEqual(reading.closing_reading, Decimal("10"))

    def test_a_keeper_may_not_delete_a_reading_on_another_meter(self):
        reading = DailyElectricityReading.objects.create(
            meter=self.terrace,
            date=date(2026, 8, 1),
            opening_reading=Decimal("0"),
            closing_reading=Decimal("10"),
        )
        response = client_for(self.keeper).delete(f"{READINGS_URL}{reading.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(DailyElectricityReading.objects.filter(pk=reading.pk).exists())

    def test_a_reading_cannot_be_moved_onto_a_meter_he_does_not_keep(self):
        """Managing only the source would let a keeper park his units elsewhere."""
        self.assertEqual(self._post_reading(self.keeper, self.boiler).status_code, 201)
        reading = DailyElectricityReading.objects.get(meter=self.boiler)
        response = client_for(self.keeper).patch(
            f"{READINGS_URL}{reading.id}/", {"meter": self.terrace.id}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        reading.refresh_from_db()
        self.assertEqual(reading.meter_id, self.boiler.id)

    def test_reading_the_register_is_never_scoped(self):
        """Attribution and the dashboards need the whole campus; only writes narrow."""
        DailyElectricityReading.objects.create(
            meter=self.terrace,
            date=date(2026, 8, 1),
            opening_reading=Decimal("0"),
            closing_reading=Decimal("10"),
        )
        client = client_for(self.keeper)
        self.assertEqual(len(client.get(METERS_URL).data), 2)
        self.assertEqual(len(client.get(READINGS_URL).data), 1)


class MeterScopeServiceTests(TestCase):
    """The helpers the pages and the report are built on."""

    def setUp(self):
        self.boiler = ElectricityMeter.objects.create(name="Boiler")
        self.terrace = ElectricityMeter.objects.create(name="Terrace")
        self.keeper = make_user("svc@example.com", "can_add_daily_electricity")
        UserElectricityMeter.objects.create(user=self.keeper, meter=self.boiler)

    def test_managed_meter_ids_is_empty_for_a_stranger(self):
        stranger = make_user("nobody@example.com")
        self.assertEqual(meter_scope.managed_meter_ids(stranger), frozenset())

    def test_manages_accepts_an_instance_or_an_id(self):
        self.assertTrue(meter_scope.manages(self.keeper, self.boiler))
        self.assertTrue(meter_scope.manages(self.keeper, self.boiler.id))
        self.assertFalse(meter_scope.manages(self.keeper, self.terrace))

    def test_users_missing_assignment_finds_the_locked_out(self):
        stranded = make_user("stranded@example.com", "can_edit_daily_electricity")
        found = {u.id for u in meter_scope.users_missing_assignment()}
        self.assertIn(stranded.id, found)
        self.assertNotIn(self.keeper.id, found)

    def test_users_missing_assignment_ignores_people_off_the_register(self):
        bystander = make_user("bystander@example.com", "can_view_daily_electricity")
        found = {u.id for u in meter_scope.users_missing_assignment()}
        self.assertNotIn(bystander.id, found)

    def test_unmanaged_meters_finds_the_meters_nobody_reads(self):
        orphans = {m.id for m in meter_scope.unmanaged_meters()}
        self.assertEqual(orphans, {self.terrace.id})

    def test_an_inactive_meter_is_not_reported_as_unkept(self):
        self.terrace.is_active = False
        self.terrace.save(update_fields=["is_active"])
        self.assertEqual(meter_scope.unmanaged_meters(), [])


class MeterManagerAPITests(TestCase):
    """The four endpoints the admin page is built on."""

    def setUp(self):
        self.boiler = ElectricityMeter.objects.create(name="Boiler")
        self.terrace = ElectricityMeter.objects.create(name="Terrace")

        self.admin = make_user("admin@example.com")
        self.admin.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="maintenance",
                codename="can_manage_user_electricity_meters",
            )
        )
        self.keeper = make_user("keeper@example.com", "can_add_daily_electricity")
        UserElectricityMeter.objects.create(user=self.keeper, meter=self.boiler)

    # ---- my-electricity-meters -------------------------------------------

    def test_any_user_can_read_their_own_meters(self):
        response = client_for(self.keeper).get(MINE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["meter_ids"], [self.boiler.id])
        self.assertEqual(response.data["meters"], [{"id": self.boiler.id, "name": "Boiler"}])
        self.assertFalse(response.data["unrestricted"])

    def test_my_meters_needs_no_admin_permission(self):
        self.assertFalse(
            self.keeper.has_perm("maintenance.can_manage_user_electricity_meters")
        )
        self.assertEqual(client_for(self.keeper).get(MINE_URL).status_code, 200)

    def test_my_meters_answers_only_about_the_caller(self):
        other = make_user("other@example.com")
        UserElectricityMeter.objects.create(user=other, meter=self.terrace)
        response = client_for(self.keeper).get(MINE_URL)
        self.assertEqual(response.data["meter_ids"], [self.boiler.id])

    def test_a_superuser_is_flagged_unrestricted(self):
        root = make_user("root@example.com", superuser=True)
        response = client_for(root).get(MINE_URL)
        self.assertTrue(response.data["unrestricted"])

    # ---- the write endpoints are admin-only ------------------------------

    def test_a_keeper_cannot_widen_their_own_scope(self):
        response = client_for(self.keeper).post(
            ASSIGN_URL, {"user": self.keeper.id, "meters": [self.terrace.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            UserElectricityMeter.objects.filter(
                user=self.keeper, meter=self.terrace
            ).exists()
        )

    def test_a_keeper_cannot_list_or_read_the_gaps(self):
        client = client_for(self.keeper)
        self.assertEqual(client.get(ASSIGN_URL).status_code, 403)
        self.assertEqual(client.get(GAPS_URL).status_code, 403)

    def test_an_admin_assigns_several_meters_at_once(self):
        response = client_for(self.admin).post(
            ASSIGN_URL,
            {"user": self.keeper.id, "meters": [self.boiler.id, self.terrace.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created"], [self.terrace.id])
        self.assertEqual(response.data["already_assigned"], [self.boiler.id])
        self.assertEqual(
            meter_scope.managed_meter_ids(self.keeper),
            frozenset({self.boiler.id, self.terrace.id}),
        )

    def test_assigning_records_who_did_it(self):
        client_for(self.admin).post(
            ASSIGN_URL, {"user": self.keeper.id, "meters": [self.terrace.id]},
            format="json",
        )
        row = UserElectricityMeter.objects.get(user=self.keeper, meter=self.terrace)
        self.assertEqual(row.created_by, self.admin)

    def test_reassigning_revives_the_old_row_rather_than_failing(self):
        """The unique constraint would refuse a duplicate; re-adding must work."""
        UserElectricityMeter.objects.filter(user=self.keeper).update(is_active=False)
        response = client_for(self.admin).post(
            ASSIGN_URL, {"user": self.keeper.id, "meters": [self.boiler.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["reactivated"], [self.boiler.id])
        self.assertEqual(UserElectricityMeter.objects.filter(user=self.keeper).count(), 1)

    def test_an_unknown_meter_is_a_400_not_a_500(self):
        response = client_for(self.admin).post(
            ASSIGN_URL, {"user": self.keeper.id, "meters": [9999]}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("No such meter", str(response.data["meters"][0]))

    def test_removing_deactivates_so_the_record_survives(self):
        row = UserElectricityMeter.objects.get(user=self.keeper, meter=self.boiler)
        response = client_for(self.admin).delete(f"{ASSIGN_URL}{row.id}/")
        self.assertEqual(response.status_code, 204)
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    def test_a_removed_assignment_can_be_restored(self):
        row = UserElectricityMeter.objects.get(user=self.keeper, meter=self.boiler)
        client_for(self.admin).delete(f"{ASSIGN_URL}{row.id}/")
        response = client_for(self.admin).patch(
            f"{ASSIGN_URL}{row.id}/", {"is_active": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_active"])

    def test_the_list_can_be_narrowed_to_one_user(self):
        UserElectricityMeter.objects.create(
            user=make_user("third@example.com"), meter=self.terrace
        )
        rows = client_for(self.admin).get(ASSIGN_URL, {"user": self.keeper.id}).data
        self.assertEqual([r["meter_name"] for r in rows], ["Boiler"])

    # ---- gaps -------------------------------------------------------------

    def test_gaps_reports_both_locked_out_people_and_unkept_meters(self):
        stranded = make_user("stranded@example.com", "can_edit_daily_electricity")
        data = client_for(self.admin).get(GAPS_URL).data
        self.assertIn(stranded.id, [u["id"] for u in data["users_without_meters"]])
        self.assertEqual(
            [m["name"] for m in data["meters_without_managers"]], ["Terrace"]
        )
