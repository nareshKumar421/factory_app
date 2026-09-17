"""The RM/PM label the gate list shows for one raw-material entry."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from gate_core.services.material_type import classify_item_codes, entry_material_type
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Vehicle
from vehicle_management.serializers import VehicleEntrySerializer


class MaterialTypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.user = user_model.objects.create_user(
            email="material-type@example.com",
            password="password",
            full_name="Material Type User",
            employee_code="MATTYPE001",
        )
        cls.company = Company.objects.create(name="Material Type Co", code="MAT_TYPE")
        cls.vehicle = Vehicle.objects.create(vehicle_number="HR55MAT01")
        cls.driver = Driver.objects.create(
            name="Material Driver",
            mobile_no="9777771001",
            license_no="MAT-DL",
        )

    def _make_entry(self, po_item_codes):
        """A raw-material entry whose POs carry ``po_item_codes`` (a list per PO)."""
        entry = VehicleEntry.objects.create(
            entry_no=f"MT-{VehicleEntry.objects.count() + 1}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED,
            created_by=self.user,
            updated_by=self.user,
        )
        for po_index, codes in enumerate(po_item_codes, start=1):
            receipt = POReceipt.objects.create(
                vehicle_entry=entry,
                po_number=f"PO-{entry.entry_no}-{po_index}",
                supplier_code="SUP",
                supplier_name="Supplier",
                created_by=self.user,
            )
            for line, code in enumerate(codes, start=1):
                POItemReceipt.objects.create(
                    po_receipt=receipt,
                    po_item_code=code,
                    item_name=f"Item {code}",
                    sap_line_num=line,
                    ordered_qty=Decimal("1.000"),
                    received_qty=Decimal("1.000"),
                    uom="KG",
                    created_by=self.user,
                )
        return entry

    def test_classify_reads_the_item_code_prefix(self):
        self.assertEqual(classify_item_codes(["RM0001", "rm0002"]), "RM")
        self.assertEqual(classify_item_codes(["PM0001", " pm0002 "]), "PM")
        self.assertEqual(classify_item_codes(["RM0001", "PM0002"]), "BOTH")

    def test_classify_without_lines_is_unknown(self):
        self.assertIsNone(classify_item_codes([]))

    def test_neither_rm_nor_pm_is_other(self):
        self.assertEqual(classify_item_codes(["FA0001", "SPARE-9"]), "OTHER")

    def test_rm_alongside_an_asset_line_still_reads_rm(self):
        # The column answers "which of RM/PM is on board"; an asset line is neither
        # and must not blank out the RM the vehicle is actually carrying.
        self.assertEqual(classify_item_codes(["RM0001", "FA0001"]), "RM")

    def test_entry_spans_every_po_on_the_vehicle(self):
        # RM on one PO and PM on another is still one mixed load.
        entry = self._make_entry([["RM0001"], ["PM0002"]])
        self.assertEqual(entry_material_type(entry), "BOTH")

    def test_entry_without_pos_has_no_material_type(self):
        entry = self._make_entry([])
        self.assertIsNone(entry_material_type(entry))

    def test_serializer_exposes_code_and_label(self):
        entry = self._make_entry([["RM0001", "PM0002"]])
        data = VehicleEntrySerializer(entry).data
        self.assertEqual(data["material_type"], {"code": "BOTH", "label": "RM + PM"})

    def test_serializer_reports_null_when_there_is_nothing_to_classify(self):
        entry = self._make_entry([])
        self.assertIsNone(VehicleEntrySerializer(entry).data["material_type"])
