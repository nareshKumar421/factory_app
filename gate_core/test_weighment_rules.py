from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from gate_core.services.weighment_rules import (
    gate_in_requires_weighment,
    gate_out_requires_weighment,
    is_rm_item_code,
    raw_material_entry_is_all_rm,
)
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Vehicle


class WeighmentRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.user = user_model.objects.create_user(
            email="weigh-rule@example.com",
            password="password",
            full_name="Weigh Rule User",
            employee_code="WEIGHRULE001",
        )
        cls.company = Company.objects.create(name="Weigh Rule Co", code="WEIGH_RULE")
        cls.vehicle = Vehicle.objects.create(vehicle_number="HR55WEIGH01")
        cls.driver = Driver.objects.create(
            name="Weigh Driver",
            mobile_no="9777770001",
            license_no="WEIGH-DL",
        )

    def _make_entry(self, entry_type, item_codes=None):
        entry = VehicleEntry.objects.create(
            entry_no=f"WR-{entry_type}-{VehicleEntry.objects.count() + 1}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type=entry_type,
            status=GateEntryStatus.COMPLETED,
            created_by=self.user,
            updated_by=self.user,
        )
        if item_codes:
            receipt = POReceipt.objects.create(
                vehicle_entry=entry,
                po_number=f"PO-{entry.entry_no}",
                supplier_code="SUP",
                supplier_name="Supplier",
                created_by=self.user,
            )
            for index, code in enumerate(item_codes, start=1):
                POItemReceipt.objects.create(
                    po_receipt=receipt,
                    po_item_code=code,
                    item_name=f"Item {code}",
                    sap_line_num=index,
                    ordered_qty=Decimal("1.000"),
                    received_qty=Decimal("1.000"),
                    uom="KG",
                    created_by=self.user,
                )
        return entry

    def test_is_rm_item_code_prefix(self):
        self.assertTrue(is_rm_item_code("RM0000016"))
        self.assertTrue(is_rm_item_code("rm-canola-oil"))
        self.assertTrue(is_rm_item_code("  RM1 "))
        self.assertFalse(is_rm_item_code("PM0000235"))
        self.assertFalse(is_rm_item_code("FG-OIL-1L"))
        self.assertFalse(is_rm_item_code(""))
        self.assertFalse(is_rm_item_code(None))

    def test_all_rm_load(self):
        entry = self._make_entry("RAW_MATERIAL", ["RM0001", "RM0002"])
        self.assertTrue(raw_material_entry_is_all_rm(entry))
        self.assertTrue(gate_out_requires_weighment(entry))
        self.assertTrue(gate_in_requires_weighment(entry))

    def test_mixed_rm_pm_load_is_optional(self):
        entry = self._make_entry("RAW_MATERIAL", ["RM0001", "PM0002"])
        self.assertFalse(raw_material_entry_is_all_rm(entry))
        self.assertFalse(gate_out_requires_weighment(entry))
        self.assertFalse(gate_in_requires_weighment(entry))

    def test_pm_only_load_is_optional(self):
        entry = self._make_entry("RAW_MATERIAL", ["PM0001", "PM0002"])
        self.assertFalse(gate_out_requires_weighment(entry))

    def test_raw_material_without_items_is_optional(self):
        entry = self._make_entry("RAW_MATERIAL")
        self.assertFalse(raw_material_entry_is_all_rm(entry))
        self.assertFalse(gate_out_requires_weighment(entry))

    def test_daily_need_never_requires_weighment(self):
        entry = self._make_entry("DAILY_NEED")
        self.assertFalse(gate_out_requires_weighment(entry))
        self.assertFalse(gate_in_requires_weighment(entry))

    def test_other_exempt_types(self):
        for entry_type in ("MAINTENANCE", "CONSTRUCTION", "FIXED_ASSET", "EMPTY_VEHICLE", "BST_IN"):
            entry = self._make_entry(entry_type)
            self.assertFalse(
                gate_out_requires_weighment(entry),
                f"{entry_type} should not require weighment",
            )

    def test_job_work_requires_weighment(self):
        entry = self._make_entry("JOB_WORK")
        self.assertTrue(gate_out_requires_weighment(entry))
        # Job work weighs at its own gate-in flow, not the raw-material one.
        self.assertFalse(gate_in_requires_weighment(entry))
