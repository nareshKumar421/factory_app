"""
Tests for the ETP / STP registers.

The value of this module is arithmetic nobody should have to re-check by hand
(flow totals, contact time, calibration variation, carry-forward openings) and
the guard rails that stop a register from lying (duplicate pages, an option
picked from the wrong dropdown, a step logged against the wrong plant). Those
are what is tested here, endpoint by endpoint.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from .constants import (
    DEFAULT_PRINT_DOCUMENTS,
    CalibrationFrequency,
    ChemicalUom,
    MonitoringStage,
    OptionCategory,
    PlantType,
    SpecValidationType,
    StaffRole,
)
from .models import (
    BackwashEntry,
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationPoint,
    CalibrationRecord,
    ChemicalConsumptionLog,
    DailyPlantLog,
    EtpPrintDocument,
    MonitoringParameter,
    MonitoringRecord,
    PlantChemical,
    PlantOption,
    PlantStaff,
    SludgeGenerationEntry,
    TreatmentPlant,
)

User = get_user_model()

ALL_PERMS = "all"


def _client(company, perms=ALL_PERMS):
    """An authenticated client holding the given ``etp.*`` permissions."""
    count = User.objects.count()
    user = User.objects.create_user(
        email=f"etp{count}@t.com",
        password="x",
        full_name=f"ETP User {count}",
        employee_code=f"E{count}",
    )
    role = UserRole.objects.create(name=f"R{UserRole.objects.count()}")
    UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    permissions = Permission.objects.filter(content_type__app_label="etp")
    if perms != ALL_PERMS:
        permissions = permissions.filter(codename__in=perms)
    user.user_permissions.set(permissions)
    user = User.objects.get(pk=user.pk)
    client = APIClient()
    client.force_authenticate(user=user)
    client.credentials(HTTP_COMPANY_CODE=company.code)
    return client


class EtpTestBase(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.plant = TreatmentPlant.objects.create(
            name="Effluent Treatment Plant", code="ETP", plant_type=PlantType.ETP
        )
        self.plant.companies.set([self.company])
        self.stp = TreatmentPlant.objects.create(
            name="Sewage Treatment Plant", code="STP", plant_type=PlantType.STP
        )
        self.operator = PlantStaff.objects.create(
            name="Anurag", role=StaffRole.OPERATOR
        )
        self.chemist = PlantStaff.objects.create(name="Anil", role=StaffRole.CHEMIST)
        self.client = _client(self.company)
        self.today = date.today()


class DailyPlantLogTests(EtpTestBase):
    URL = "/api/v1/etp/daily-logs/"

    def _post(self, **overrides):
        payload = {
            "plant": self.plant.id,
            "date": str(self.today),
            "inlet_initial": "7986.05",
            "inlet_final": "8002.95",
            "outlet_initial": "7451.67",
            "outlet_final": "7467.67",
            "ph_reading": "7.84",
            "energy_initial": "766",
            "energy_final": "796",
            "operator": self.operator.id,
        }
        payload.update(overrides)
        return self.client.post(self.URL, payload, format="json")

    def test_totals_are_derived_from_the_meter_pairs(self):
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = DailyPlantLog.objects.get(id=response.data["id"])
        self.assertEqual(str(log.inlet_total), "16.90")
        self.assertEqual(str(log.outlet_total), "16.00")
        self.assertEqual(str(log.energy_units), "30.00")

    def test_openings_carry_forward_from_the_previous_day(self):
        self._post(date=str(self.today - timedelta(days=1)))
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "inlet_final": "8019.65",
                "outlet_final": "7483.77",
                "energy_final": "825",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = DailyPlantLog.objects.get(id=response.data["id"])
        # Yesterday's closings became today's openings, untyped.
        self.assertEqual(str(log.inlet_initial), "8002.95")
        self.assertEqual(str(log.outlet_initial), "7467.67")
        self.assertEqual(str(log.energy_initial), "796.00")
        self.assertEqual(str(log.inlet_total), "16.70")

    def test_first_ever_log_needs_its_own_openings(self):
        # Nothing to carry forward: the totals simply stay at zero rather than
        # the entry being refused, so a plant can start mid-month.
        response = self._post(inlet_initial=None, outlet_initial=None, energy_initial=None)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = DailyPlantLog.objects.get(id=response.data["id"])
        self.assertEqual(str(log.inlet_total), "0.00")

    def test_second_log_for_the_same_day_is_refused(self):
        self._post()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("date", response.data)

    def test_closing_below_opening_is_refused(self):
        response = self._post(inlet_final="7000.00")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("inlet_final", response.data)

    def test_last_readings_endpoint_prefills_the_next_day(self):
        self._post()
        response = self.client.get(
            "/api/v1/etp/daily-logs/last-readings/", {"plant": self.plant.id}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["found"])
        self.assertEqual(str(response.data["inlet_final"]), "8002.95")


class MonitoringTests(EtpTestBase):
    URL = "/api/v1/etp/monitoring-records/"

    def setUp(self):
        super().setUp()
        self.ph = MonitoringParameter.objects.create(
            plant=self.plant,
            stage=MonitoringStage.TREATED,
            parameter_key="ph",
            parameter_name="pH",
            min_value="6.5",
            max_value="8.5",
            validation_type=SpecValidationType.RANGE,
        )
        self.do = MonitoringParameter.objects.create(
            plant=self.plant,
            stage=MonitoringStage.TREATED,
            parameter_key="do",
            parameter_name="DO",
            unit="ppm",
            min_value="2.0",
            validation_type=SpecValidationType.MIN,
        )
        self.other_plant_param = MonitoringParameter.objects.create(
            plant=self.stp,
            stage=MonitoringStage.INFLUENT,
            parameter_key="ph",
            parameter_name="pH",
        )

    def _sheet(self, ph_value="7.44", do_value="2.4"):
        return {
            "plant": self.plant.id,
            "date": str(self.today),
            "interval_hours": 2,
            "chemist": self.chemist.id,
            "readings": [
                {
                    "reading_time": "14:00",
                    "operator": self.operator.id,
                    "values": [
                        {"parameter": self.ph.id, "value": ph_value},
                        {"parameter": self.do.id, "value": do_value},
                    ],
                }
            ],
        }

    def test_sheet_saves_its_grid(self):
        response = self.client.post(self.URL, self._sheet(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        record = MonitoringRecord.objects.get(id=response.data["id"])
        self.assertEqual(record.readings.count(), 1)
        self.assertEqual(record.readings.first().values.count(), 2)
        self.assertEqual(response.data["out_of_spec_count"], 0)

    def test_value_outside_the_configured_limits_is_flagged_not_refused(self):
        response = self.client.post(
            self.URL, self._sheet(ph_value="9.60", do_value="1.0"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["out_of_spec_count"], 2)

    def test_editing_the_sheet_replaces_its_rows(self):
        created = self.client.post(self.URL, self._sheet(), format="json")
        record_id = created.data["id"]
        sheet = self._sheet()
        sheet["readings"].append(
            {
                "reading_time": "16:00",
                "values": [{"parameter": self.ph.id, "value": "7.42"}],
            }
        )
        response = self.client.patch(
            f"{self.URL}{record_id}/", sheet, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(MonitoringRecord.objects.get(id=record_id).readings.count(), 2)

    def test_two_rows_at_the_same_time_are_refused(self):
        sheet = self._sheet()
        sheet["readings"].append(sheet["readings"][0])
        response = self.client.post(self.URL, sheet, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_parameter_from_another_plant_is_refused(self):
        sheet = self._sheet()
        sheet["readings"][0]["values"].append(
            {"parameter": self.other_plant_param.id, "value": "7.0"}
        )
        response = self.client.post(self.URL, sheet, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("readings", response.data)

    def test_sheet_template_lists_columns_and_time_slots(self):
        response = self.client.get(
            f"{self.URL}sheet-template/",
            {"plant": self.plant.id, "interval_hours": 2, "start_hour": 6},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["parameters"]), 2)
        self.assertEqual(response.data["time_slots"][0], "06:00")
        self.assertEqual(len(response.data["time_slots"]), 12)

    def test_verify_stamps_the_sheet(self):
        created = self.client.post(self.URL, self._sheet(), format="json")
        response = self.client.post(
            f"{self.URL}{created.data['id']}/verify/",
            {"verified_by": self.chemist.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["is_verified"])

    def test_replacing_the_grid_is_logged_as_a_readings_change(self):
        created = self.client.post(self.URL, self._sheet(), format="json")
        self.client.patch(
            f"{self.URL}{created.data['id']}/",
            self._sheet(ph_value="7.90"),
            format="json",
        )
        rows = self.client.get(
            "/api/v1/etp/change-log/", {"register": "MONITORING", "action": "UPDATED"}
        ).data
        self.assertEqual(len(rows), 1)
        self.assertIn("readings", rows[0]["changes"])
        self.assertIn("7.44", rows[0]["changes"]["readings"]["from"])
        self.assertIn("7.90", rows[0]["changes"]["readings"]["to"])

    def test_verifying_is_logged(self):
        created = self.client.post(self.URL, self._sheet(), format="json")
        self.client.post(
            f"{self.URL}{created.data['id']}/verify/",
            {"verified_by": self.chemist.id},
            format="json",
        )
        rows = self.client.get(
            "/api/v1/etp/change-log/", {"register": "MONITORING", "action": "VERIFIED"}
        ).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "Verified")

    def test_verify_needs_the_verify_permission(self):
        created = self.client.post(self.URL, self._sheet(), format="json")
        recorder = _client(
            self.company,
            perms=["can_view_etp_monitoring", "can_manage_etp_monitoring"],
        )
        response = recorder.post(
            f"{self.URL}{created.data['id']}/verify/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ChemicalConsumptionTests(EtpTestBase):
    URL = "/api/v1/etp/chemical-logs/"

    def setUp(self):
        super().setUp()
        self.hypo = PlantChemical.objects.create(
            name="HYPO", default_uom=ChemicalUom.LTR, sequence=1
        )
        self.dap = PlantChemical.objects.create(
            name="DAP", default_uom=ChemicalUom.KG, sequence=2
        )

    def test_lines_snapshot_the_units_and_skip_blank_cells(self):
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "operator": self.operator.id,
                "lines": [
                    {"chemical": self.hypo.id, "quantity": "2.000"},
                    {"chemical": self.dap.id, "quantity": None},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = ChemicalConsumptionLog.objects.get(id=response.data["id"])
        self.assertEqual(log.lines.count(), 1)
        self.assertEqual(log.lines.first().uom, ChemicalUom.LTR)

    def test_unit_may_be_overridden_on_the_line(self):
        # The STP form records HYPO in grams even though the master says litres.
        response = self.client.post(
            self.URL,
            {
                "plant": self.stp.id,
                "date": str(self.today),
                "lines": [
                    {"chemical": self.hypo.id, "quantity": "410", "uom": "GM"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = ChemicalConsumptionLog.objects.get(id=response.data["id"])
        self.assertEqual(log.lines.first().uom, ChemicalUom.GM)

    def test_same_chemical_twice_is_refused(self):
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "lines": [
                    {"chemical": self.hypo.id, "quantity": "1"},
                    {"chemical": self.hypo.id, "quantity": "2"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lines", response.data)

    def test_totals_endpoint_sums_the_window(self):
        for day, quantity in ((0, "2.000"), (1, "3.000")):
            self.client.post(
                self.URL,
                {
                    "plant": self.plant.id,
                    "date": str(self.today - timedelta(days=day)),
                    "lines": [{"chemical": self.hypo.id, "quantity": quantity}],
                },
                format="json",
            )
        response = self.client.get(
            f"{self.URL}totals/",
            {
                "plant": self.plant.id,
                "date_from": str(self.today - timedelta(days=7)),
                "date_to": str(self.today),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(Decimal(response.data[0]["total"]), Decimal("5"))


class SludgeTests(EtpTestBase):
    URL = "/api/v1/etp/sludge-entries/"

    def setUp(self):
        super().setUp()
        self.filter_press = PlantOption.objects.create(
            category=OptionCategory.SLUDGE_COLLECTION_MODE, label="Filter Press"
        )
        self.bag = PlantOption.objects.create(
            category=OptionCategory.SLUDGE_STORAGE_METHOD, label="Bag"
        )

    def test_serial_numbers_continue_the_paper_register(self):
        first = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "quantity_kg": "85.00",
                "collection_mode": self.filter_press.id,
                "storage_method": self.bag.id,
            },
            format="json",
        )
        second = self.client.post(
            self.URL,
            {"plant": self.plant.id, "date": str(self.today), "quantity_kg": "80.00"},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(second.data["serial_no"], first.data["serial_no"] + 1)

    def test_option_from_the_wrong_dropdown_is_refused(self):
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                # 'Bag' is a storage method, not a collection mode.
                "collection_mode": self.bag.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("collection_mode", response.data)

    def test_quantity_may_be_left_blank(self):
        # Two lines of the paper register carry a date and a source but no
        # weight; the register has to be able to say the same.
        response = self.client.post(
            self.URL,
            {"plant": self.stp.id, "date": str(self.today)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(
            SludgeGenerationEntry.objects.get(id=response.data["id"]).quantity_kg
        )


class BackwashTests(EtpTestBase):
    URL = "/api/v1/etp/backwash-entries/"

    def setUp(self):
        super().setUp()
        self.step = BackwashEquipment.objects.create(
            plant=self.plant, name="Sand Filter Backwash", sequence=1
        )
        self.other_step = BackwashEquipment.objects.create(
            plant=self.stp, name="Carbon Filter Rinse", sequence=1
        )

    def test_contact_time_is_derived(self):
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "equipment": self.step.id,
                "start_time": "08:20",
                "stop_time": "08:30",
                "operator": self.operator.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["contact_minutes"], 10)

    def test_a_wash_across_midnight_is_not_negative(self):
        entry = BackwashEntry.objects.create(
            plant=self.plant,
            date=self.today,
            equipment=self.step,
            start_time="23:50",
            stop_time="00:05",
        )
        self.assertEqual(entry.contact_minutes, 15)

    def test_step_belonging_to_another_plant_is_refused(self):
        response = self.client.post(
            self.URL,
            {
                "plant": self.plant.id,
                "date": str(self.today),
                "equipment": self.other_step.id,
                "start_time": "08:20",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("equipment", response.data)

    def test_the_same_step_at_the_same_time_is_refused(self):
        payload = {
            "plant": self.plant.id,
            "date": str(self.today),
            "equipment": self.step.id,
            "start_time": "08:20",
        }
        self.client.post(self.URL, payload, format="json")
        response = self.client.post(self.URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CalibrationTests(EtpTestBase):
    URL = "/api/v1/etp/calibration-records/"

    def setUp(self):
        super().setUp()
        self.instrument = CalibrationInstrument.objects.create(
            plant=self.plant,
            equipment_name="pH Meter",
            equipment_id="ETP-LE-01",
            working_range="0 - 14",
            frequency=CalibrationFrequency.WEEKLY,
            tolerance="0.200",
            standard_make_model="ADVIT PH14",
        )
        for index, value in enumerate(("4.000", "7.000", "10.010"), start=1):
            CalibrationPoint.objects.create(
                instrument=self.instrument, actual_value=value, sequence=index
            )

    def test_variation_and_verdict_are_derived(self):
        response = self.client.post(
            self.URL,
            {
                "instrument": self.instrument.id,
                "date": str(self.today),
                "time": "01:00",
                "checked_by": self.chemist.id,
                "readings": [
                    {"actual_value": "4.000", "observed_value": "3.980"},
                    {"actual_value": "7.000", "observed_value": "6.820"},
                    {"actual_value": "10.010", "observed_value": "9.920"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        record = CalibrationRecord.objects.get(id=response.data["id"])
        variations = [str(r.variation) for r in record.readings.all()]
        self.assertEqual(variations, ["-0.020", "-0.180", "-0.090"])
        self.assertFalse(record.is_out_of_calibration)

    def test_a_reading_beyond_the_tolerance_flags_the_instrument(self):
        response = self.client.post(
            self.URL,
            {
                "instrument": self.instrument.id,
                "date": str(self.today),
                "readings": [{"actual_value": "7.000", "observed_value": "7.500"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            CalibrationRecord.objects.get(id=response.data["id"]).is_out_of_calibration
        )

    def test_due_date_comes_from_the_instrument_frequency(self):
        response = self.client.post(
            self.URL,
            {"instrument": self.instrument.id, "date": str(self.today)},
            format="json",
        )
        record = CalibrationRecord.objects.get(id=response.data["id"])
        self.assertEqual(record.due_date, self.today + timedelta(days=7))

    def test_omitting_readings_lays_out_the_configured_buffer_points(self):
        response = self.client.post(
            self.URL,
            {"instrument": self.instrument.id, "date": str(self.today)},
            format="json",
        )
        record = CalibrationRecord.objects.get(id=response.data["id"])
        self.assertEqual(
            [str(r.actual_value) for r in record.readings.all()],
            ["4.000", "7.000", "10.010"],
        )


class MasterAndPermissionTests(EtpTestBase):
    def test_a_register_operator_cannot_edit_the_masters(self):
        operator = _client(
            self.company,
            perms=["can_view_etp_daily_log", "can_manage_etp_daily_log"],
        )
        # Reading a master is allowed — the daily-log form needs the plant list.
        self.assertEqual(
            operator.get("/api/v1/etp/plants/").status_code, status.HTTP_200_OK
        )
        response = operator.post(
            "/api/v1/etp/plants/", {"name": "New", "code": "NEW"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_viewer_cannot_file_an_entry(self):
        viewer = _client(self.company, perms=["can_view_etp_daily_log"])
        self.assertEqual(
            viewer.get("/api/v1/etp/daily-logs/").status_code, status.HTTP_200_OK
        )
        response = viewer.post(
            "/api/v1/etp/daily-logs/",
            {"plant": self.plant.id, "date": str(self.today)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_holder_of_one_register_cannot_read_another(self):
        operator = _client(self.company, perms=["can_view_etp_daily_log"])
        self.assertEqual(
            operator.get("/api/v1/etp/calibration-records/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_plant_in_use_cannot_be_deleted(self):
        DailyPlantLog.objects.create(plant=self.plant, date=self.today)
        response = self.client.delete(f"/api/v1/etp/plants/{self.plant.id}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("inactive", response.data["detail"])

    def test_company_filter_drops_plants_of_other_companies(self):
        response = self.client.get("/api/v1/etp/plants/", {"company": self.company.code})
        codes = [row["code"] for row in response.data]
        self.assertIn("ETP", codes)
        self.assertNotIn("STP", codes)  # untagged plant is not attributed anywhere


class OverviewTests(EtpTestBase):
    def test_dashboard_reports_what_is_still_unfilled_today(self):
        response = self.client.get("/api/v1/etp/dashboard/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        card = next(c for c in response.data["plants"] if c["plant_code"] == "ETP")
        self.assertFalse(card["daily_log_done"])

        DailyPlantLog.objects.create(
            plant=self.plant, date=self.today, inlet_initial=1, inlet_final=5
        )
        response = self.client.get("/api/v1/etp/dashboard/")
        card = next(c for c in response.data["plants"] if c["plant_code"] == "ETP")
        self.assertTrue(card["daily_log_done"])

    def test_dashboard_lists_an_instrument_never_calibrated(self):
        CalibrationInstrument.objects.create(
            plant=self.plant, equipment_name="pH Meter", equipment_id="ETP-LE-09"
        )
        response = self.client.get("/api/v1/etp/dashboard/")
        due_codes = [row["equipment_id"] for row in response.data["calibration_due"]]
        self.assertIn("ETP-LE-09", due_codes)

    def test_summary_totals_the_window(self):
        for day in range(3):
            DailyPlantLog.objects.create(
                plant=self.plant,
                date=self.today - timedelta(days=day),
                inlet_initial=0,
                inlet_final=10,
                energy_initial=0,
                energy_final=30,
            )
        response = self.client.get(
            "/api/v1/etp/summary/",
            {
                "plant": self.plant.id,
                "date_from": str(self.today - timedelta(days=7)),
                "date_to": str(self.today),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["days_logged"], 3)
        self.assertEqual(Decimal(response.data["inlet_kl"]), Decimal("30"))
        self.assertEqual(Decimal(response.data["energy_units"]), Decimal("90"))


class ChangeLogTests(EtpTestBase):
    """The registers stay editable, so every edit has to be attributable."""

    URL = "/api/v1/etp/change-log/"
    LOG_URL = "/api/v1/etp/daily-logs/"

    def _record_day(self, **overrides):
        payload = {
            "plant": self.plant.id,
            "date": str(self.today),
            "inlet_initial": "100.00",
            "inlet_final": "120.00",
            "ph_reading": "7.84",
            "operator": self.operator.id,
        }
        payload.update(overrides)
        return self.client.post(self.LOG_URL, payload, format="json")

    def _trail(self, **params):
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_recording_a_day_opens_the_trail(self):
        created = self._record_day()
        rows = self._trail(register="DAILY_LOG")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "CREATED")
        self.assertEqual(rows[0]["object_id"], created.data["id"])
        self.assertEqual(rows[0]["entry_date"], str(self.today))
        self.assertEqual(rows[0]["plant_code"], "ETP")
        self.assertTrue(rows[0]["changed_by_name"])

    def test_editing_records_what_moved_and_what_it_was(self):
        created = self._record_day()
        response = self.client.patch(
            f"{self.LOG_URL}{created.data['id']}/",
            {"ph_reading": "7.60", "remarks": "corrected at review"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        rows = self._trail(register="DAILY_LOG", action="UPDATED")
        self.assertEqual(len(rows), 1)
        changes = rows[0]["changes"]
        self.assertEqual(changes["ph_reading"], {"from": "7.84", "to": "7.60"})
        self.assertEqual(changes["remarks"]["to"], "corrected at review")
        self.assertIn("pH reading 7.84 → 7.60", rows[0]["summary"])

    def test_a_save_that_changed_nothing_is_not_logged(self):
        created = self._record_day()
        self.client.patch(
            f"{self.LOG_URL}{created.data['id']}/", {"ph_reading": "7.84"}, format="json"
        )
        self.assertEqual(self._trail(register="DAILY_LOG", action="UPDATED"), [])

    def test_deleting_a_day_leaves_the_trail_behind(self):
        created = self._record_day()
        entry_id = created.data["id"]
        self.client.delete(f"{self.LOG_URL}{entry_id}/")
        rows = self._trail(register="DAILY_LOG", action="DELETED")
        self.assertEqual(len(rows), 1)
        # The row is gone but the trail still points at what it was.
        self.assertEqual(rows[0]["object_id"], entry_id)
        self.assertEqual(rows[0]["entry_date"], str(self.today))
        self.assertFalse(DailyPlantLog.objects.filter(id=entry_id).exists())

    def test_derived_totals_show_up_as_changes_too(self):
        created = self._record_day()
        self.client.patch(
            f"{self.LOG_URL}{created.data['id']}/",
            {"inlet_final": "130.00"},
            format="json",
        )
        changes = self._trail(register="DAILY_LOG", action="UPDATED")[0]["changes"]
        self.assertEqual(changes["inlet_final"], {"from": "120.00", "to": "130.00"})
        self.assertEqual(changes["inlet_total"], {"from": "20.00", "to": "30.00"})

    def test_trail_can_be_narrowed_to_one_entry(self):
        first = self._record_day()
        self._record_day(date=str(self.today - timedelta(days=1)))
        rows = self._trail(object_id=first.data["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["object_id"], first.data["id"])

    def test_the_trail_cannot_be_written_to(self):
        self._record_day()
        response = self.client.post(self.URL, {"summary": "nope"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        rows = self._trail()
        response = self.client.delete(f"{self.URL}{rows[0]['id']}/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_someone_outside_the_module_cannot_read_the_trail(self):
        self._record_day()
        outsider = _client(self.company, perms=[])
        self.assertEqual(
            outsider.get(self.URL).status_code, status.HTTP_403_FORBIDDEN
        )


class PrintDocumentTests(EtpTestBase):
    """The numbers the registers print live in the database, not in the bundle."""

    URL = "/api/v1/etp/print-documents/"

    def _payload(self, **overrides):
        payload = {
            "document_key": "ETP_SLUDGE_GENERATION",
            "form_name": "SLUDGE GENERATION RECORD",
            "document_code": "QA-FRM-14-00-08-06",
            "revision": "00",
            "issue_date": str(self.today),
        }
        payload.update(overrides)
        return payload

    def test_a_number_can_be_set_and_corrected_without_a_release(self):
        created = self.client.post(self.URL, self._payload(), format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertIsNone(created.data["company_code"])  # factory-wide by default

        fixed = self.client.patch(
            f"{self.URL}{created.data['id']}/",
            {"document_code": "QA-FRM-14-00-08-07", "revision": "01"},
            format="json",
        )
        self.assertEqual(fixed.status_code, status.HTTP_200_OK, fixed.data)
        row = EtpPrintDocument.objects.get(id=created.data["id"])
        self.assertEqual(row.document_code, "QA-FRM-14-00-08-07")
        self.assertEqual(row.revision, "01")

    def test_the_same_form_cannot_be_numbered_twice_for_one_scope(self):
        self.client.post(self.URL, self._payload(), format="json")
        clash = self.client.post(self.URL, self._payload(), format="json")
        self.assertEqual(clash.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_key", clash.data)

    def test_a_company_may_override_the_factory_wide_number(self):
        self.client.post(self.URL, self._payload(), format="json")
        override = self.client.post(
            self.URL,
            self._payload(company_code=self.company.code, document_code="ETP-LOCAL-01"),
            format="json",
        )
        self.assertEqual(override.status_code, status.HTTP_201_CREATED, override.data)
        self.assertEqual(override.data["company_code"], self.company.code)

        # A company-filtered read returns its own row plus the default it overrides.
        rows = self.client.get(self.URL, {"company": self.company.code}).data
        self.assertEqual(len(rows), 2)

    def test_the_form_list_is_offered_for_the_settings_picker(self):
        response = self.client.get(f"{self.URL}keys/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        keys = [row["value"] for row in response.data]
        self.assertIn("ETP_SLUDGE_GENERATION", keys)
        self.assertIn("STP_CHEMICAL_CONSUMPTION", keys)

    def test_an_operator_can_read_the_numbers_but_not_change_them(self):
        self.client.post(self.URL, self._payload(), format="json")
        operator = _client(
            self.company,
            perms=["can_view_etp_daily_log", "can_manage_etp_daily_log"],
        )
        # A print has to be able to read its own number.
        self.assertEqual(operator.get(self.URL).status_code, status.HTTP_200_OK)
        refused = operator.post(self.URL, self._payload(document_key="ETP_DAILY_RECORD"), format="json")
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)

    def test_fix_existing_adopts_a_hand_typed_chemical_instead_of_twinning_it(self):
        """A factory that typed masters in before the seeder ever ran.

        Ganaur's database had a hand-made ``HCL`` row in grams. Seeding without
        adoption leaves it beside ``HCL (Hydrochloric Acid)`` and the consumption
        register prints two columns for the one chemical.
        """
        typed = PlantChemical.objects.create(name="HCL", default_uom=ChemicalUom.GM)

        call_command("seed_etp_masters", "--skip-people", "--fix-existing", verbosity=0)

        typed.refresh_from_db()
        self.assertEqual(typed.name, "HCL (Hydrochloric Acid)")
        # The unit follows the ETP sheet, which records HCL in litres.
        self.assertEqual(typed.default_uom, ChemicalUom.LTR)
        self.assertEqual(
            PlantChemical.objects.filter(
                name__startswith="HCL", is_active=True
            ).count(),
            1,
        )

    def test_fix_existing_fills_a_thin_hand_made_plant_row(self):
        """Ganaur's row was typed in Settings as just the code, with no location."""
        TreatmentPlant.objects.filter(pk=self.stp.pk).update(name="STP", location="")

        call_command("seed_etp_masters", "--skip-people", "--fix-existing", verbosity=0)

        self.stp.refresh_from_db()
        self.assertEqual(self.stp.name, "Sewage Treatment Plant")
        self.assertEqual(self.stp.location, "STP Plant, Ganaur")

    def test_fix_existing_leaves_a_name_a_person_chose_alone(self):
        TreatmentPlant.objects.filter(pk=self.plant.pk).update(
            name="Ganaur ETP (South)", location="Behind the tank farm"
        )

        call_command("seed_etp_masters", "--skip-people", "--fix-existing", verbosity=0)

        self.plant.refresh_from_db()
        self.assertEqual(self.plant.name, "Ganaur ETP (South)")
        self.assertEqual(self.plant.location, "Behind the tank farm")

    def test_the_seeder_fills_every_form_from_the_paper_registers(self):
        call_command("seed_etp_masters", "--skip-people", verbosity=0)
        rows = {
            row.document_key: row.document_code
            for row in EtpPrintDocument.objects.filter(company__isnull=True)
        }
        self.assertEqual(len(rows), len(DEFAULT_PRINT_DOCUMENTS))
        # The codes read off the controlled originals.
        self.assertEqual(rows["ETP_SLUDGE_GENERATION"], "QA-FRM-14-00-08-06")
        self.assertEqual(rows["ETP_BACKWASH_RECORD"], "QA-FRM-14-09-00-03")
        self.assertEqual(rows["ETP_CALIBRATION_RECORD"], "CAL-FRM-08-03-00-01")
