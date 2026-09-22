"""Who a day's electricity belongs to, and when the dial was actually read.

Two things the register could not say before:

* **who used it.** Attribution was read off the meter master, so editing a
  meter silently re-attributed every reading ever taken on it, and a day the
  line ran for somebody else could not be recorded at all. A reading now
  carries its own copy, taken from the meter when it is entered; only history
  from before that falls back to the meter.
* **when.** ``created_at`` says when the row was typed, which on the morning
  round is hours after the dial was looked at.

Sidle is the third thing here: it draws off the factory's supply without being
a Jivo company, so it is offered in the same picker as the companies and
filtered the same way, but it is NOT in the company master — a row there would
put it in every company dropdown in the ERP.
"""

from datetime import date, time
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from company.models import Company
from maintenance.models import (
    DailyElectricityReading,
    ElectricityConsumer,
    ElectricityMeter,
)
from maintenance.models_manager import UserElectricityMeter
from maintenance.tests_meter_scope import client_for, make_user

READINGS_URL = "/api/v1/maintenance/daily-electricity-readings/"
CONSUMERS_URL = "/api/v1/maintenance/electricity-consumers/"
METERS_URL = "/api/v1/maintenance/electricity-meters/"

EVERY_RIGHT = (
    "can_view_daily_electricity",
    "can_view_electricity_meter",
    "can_manage_electricity_meter",
    "can_add_daily_electricity",
    "can_edit_daily_electricity",
    "can_delete_daily_electricity",
)

DAY = date(2026, 6, 15)


class ElectricityRegisterFixture(TestCase):
    """One meter feeding both companies, kept by one keeper."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.beverages = Company.objects.create(
            name="Jivo Beverages", code="JIVO_BEVERAGES"
        )
        # Not a company, and deliberately not in the company master.
        self.sidle = ElectricityConsumer.objects.create(name="Sidle", code="SIDLE")

        self.meter = ElectricityMeter.objects.create(
            name="KWH", rate_per_unit=Decimal("9"), multiplying_factor=Decimal("10")
        )
        self.meter.companies.set([self.oil, self.beverages])

        self.keeper = make_user("keeper@example.com", *EVERY_RIGHT)
        UserElectricityMeter.objects.create(user=self.keeper, meter=self.meter)
        self.client = client_for(self.keeper)

    def post_reading(self, **extra):
        payload = {
            "meter": self.meter.id,
            "date": str(DAY),
            "opening_reading": "100",
            "closing_reading": "200",
        }
        payload.update(extra)
        return self.client.post(READINGS_URL, payload, format="json")

    def make_untagged_reading(self, day=DAY):
        """History: a reading from before the form asked who it was for."""
        return DailyElectricityReading.objects.create(
            meter=self.meter,
            date=day,
            opening_reading=Decimal("0"),
            closing_reading=Decimal("100"),
            multiplying_factor=Decimal("10"),
            rate_per_unit=Decimal("9"),
        )


class ReadingAttributionTests(ElectricityRegisterFixture):
    def test_a_new_reading_copies_the_meters_companies(self):
        response = self.post_reading()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertCountEqual(
            response.data["company_codes"], ["JIVO_OIL", "JIVO_BEVERAGES"]
        )
        reading = DailyElectricityReading.objects.get(pk=response.data["id"])
        self.assertCountEqual(
            [c.code for c in reading.companies.all()], ["JIVO_OIL", "JIVO_BEVERAGES"]
        )

    def test_the_form_may_narrow_the_day_to_one_company(self):
        """The line ran for Beverages alone that day — that is the whole point."""
        response = self.post_reading(company_codes=["JIVO_BEVERAGES"])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["company_codes"], ["JIVO_BEVERAGES"])
        self.assertEqual(response.data["attribution_display"], "Jivo Beverages")

    def test_a_day_may_be_attributed_to_sidle_without_a_company(self):
        response = self.post_reading(company_codes=[], consumer_codes=["SIDLE"])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["company_codes"], [])
        self.assertEqual(response.data["consumer_codes"], ["SIDLE"])
        self.assertEqual(response.data["attribution_display"], "Sidle")

    def test_sidle_and_a_company_can_share_a_day(self):
        response = self.post_reading(
            company_codes=["JIVO_OIL"], consumer_codes=["SIDLE"]
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["attribution_display"], "Jivo Oil, Sidle")

    def test_editing_the_meter_master_does_not_re_attribute_old_readings(self):
        """The bug this field exists for: history must not move under people."""
        created = self.post_reading()
        self.meter.companies.set([self.oil])

        reading = DailyElectricityReading.objects.get(pk=created.data["id"])
        self.assertCountEqual(
            [c.code for c in reading.companies.all()], ["JIVO_OIL", "JIVO_BEVERAGES"]
        )

    def test_a_reading_that_names_nobody_falls_back_to_its_meter(self):
        reading = self.make_untagged_reading()
        response = self.client.get(f"{READINGS_URL}{reading.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["company_codes"], [])
        self.assertEqual(
            response.data["attribution_display"], "Jivo Oil, Jivo Beverages"
        )

    def test_an_unknown_company_code_is_refused(self):
        response = self.post_reading(company_codes=["NOT_A_COMPANY"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("company_codes", response.data)


class AttributionFilterTests(ElectricityRegisterFixture):
    """The register's Company dropdown, which now follows the reading."""

    def codes_for(self, company):
        response = self.client.get(READINGS_URL, {"company": company})
        self.assertEqual(response.status_code, 200)
        return [row["id"] for row in response.data]

    def test_the_filter_follows_the_reading_not_the_meter(self):
        beverages_day = self.post_reading(company_codes=["JIVO_BEVERAGES"])
        self.assertEqual(beverages_day.status_code, 201, beverages_day.data)
        reading_id = beverages_day.data["id"]

        self.assertIn(reading_id, self.codes_for("JIVO_BEVERAGES"))
        # The METER still feeds Oil; this day did not.
        self.assertNotIn(reading_id, self.codes_for("JIVO_OIL"))

    def test_untagged_history_is_still_found_through_its_meter(self):
        reading = self.make_untagged_reading(day=date(2026, 6, 14))
        self.assertIn(reading.id, self.codes_for("JIVO_OIL"))
        self.assertIn(reading.id, self.codes_for("JIVO_BEVERAGES"))

    def test_sidle_filters_like_a_company(self):
        sidle_day = self.post_reading(company_codes=[], consumer_codes=["SIDLE"])
        self.assertEqual(sidle_day.status_code, 201, sidle_day.data)

        self.assertEqual(self.codes_for("SIDLE"), [sidle_day.data["id"]])
        self.assertEqual(self.codes_for("JIVO_OIL"), [])

    def test_a_meter_can_be_filtered_by_the_consumer_it_feeds(self):
        self.meter.consumers.set([self.sidle])
        response = self.client.get(METERS_URL, {"company": "SIDLE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.meter.id])


class ReadingTimeTests(ElectricityRegisterFixture):
    def test_the_time_the_dial_was_read_is_stored_as_entered(self):
        response = self.post_reading(reading_time="06:30:00")
        self.assertEqual(response.status_code, 201, response.data)
        reading = DailyElectricityReading.objects.get(pk=response.data["id"])
        self.assertEqual(reading.reading_time, time(6, 30))

    def test_an_omitted_time_is_filled_in_rather_than_left_blank(self):
        """"When was this read?" must always have an answer going forward."""
        before = timezone.localtime().time()
        response = self.post_reading()
        reading = DailyElectricityReading.objects.get(pk=response.data["id"])
        self.assertIsNotNone(reading.reading_time)
        self.assertGreaterEqual(reading.reading_time, before)

    def test_history_without_a_time_reads_back_as_null(self):
        reading = self.make_untagged_reading()
        response = self.client.get(f"{READINGS_URL}{reading.id}/")
        self.assertIsNone(response.data["reading_time"])


class ElectricityConsumerEndpointTests(ElectricityRegisterFixture):
    def test_the_picker_is_served_the_active_consumers(self):
        response = self.client.get(CONSUMERS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["code"] for row in response.data], ["SIDLE"])

    def test_a_retired_consumer_is_not_offered(self):
        ElectricityConsumer.objects.filter(code="SIDLE").update(is_active=False)
        response = self.client.get(CONSUMERS_URL)
        self.assertEqual(response.data, [])

    def test_the_list_is_not_editable_over_the_api(self):
        """It is admin-kept on purpose: a picker that invents its own options
        is how one tenant ends up spelled three ways."""
        response = self.client.post(
            CONSUMERS_URL, {"name": "Someone", "code": "SOMEONE"}, format="json"
        )
        self.assertEqual(response.status_code, 405)
