from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
)
from vehicle_management.models import Vehicle, VehicleType


class ScanSkipCompanyResolutionTests(TestCase):
    """Scan-skip endpoints resolve the docking by the user's companies, not the
    active Company-Code header.

    Regression: ``get_sales_dispatch_or_404`` was switched to take the request, but
    docking_admin still passed the header ``Company`` object, so ``user_company_ids``
    did ``company.user`` -> AttributeError -> 500.
    """

    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="scanskip@example.com",
            password="testpass123",
            full_name="Scan Skip",
            employee_code="SS001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="docking_admin")
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-SS")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01SS0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Scan Driver", mobile_no="9000000001", license_no="DL-SS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _docking(self, company, suffix):
        entry = VehicleEntry.objects.create(
            entry_no=f"SSV-{suffix}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"SSDOCK-{suffix}", vehicle_entry=entry,
            vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
            status="DOCKED", created_by=self.user, updated_by=self.user,
        )

    def test_by_sales_dispatch_resolves_across_companies(self):
        # Docking belongs to Beverages; active header is Oil. The user is in both,
        # so the lookup resolves the docking by record -> 200 (previously 500).
        dock = self._docking(self.beverages, "501")
        response = self.client.get(
            f"/api/v1/docking-admin/scan-skip-requests/by-sales-dispatch/{dock.id}/",
            HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(response.status_code, 200)

    def test_by_sales_dispatch_out_of_scope_company_404(self):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")  # user NOT a member
        dock = self._docking(mart, "502")
        response = self.client.get(
            f"/api/v1/docking-admin/scan-skip-requests/by-sales-dispatch/{dock.id}/",
            HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(response.status_code, 404)


@override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=[])
class PartialScanApprovalTests(TestCase):
    """Partial-dispatch approval: dispatch a docking with some-but-not-all boxes
    scanned needs an admin approval, mirroring the zero-scan scan-skip flow."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="partial@example.com",
            password="testpass123",
            full_name="Partial User",
            employee_code="PS001",
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="docking_admin")
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-PS")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01PS0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Partial Driver", mobile_no="9000000002", license_no="DL-PS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _docking(self, suffix, total_boxes):
        entry = VehicleEntry.objects.create(
            entry_no=f"PSV-{suffix}", company=self.oil, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.oil, entry_no=f"PSDOCK-{suffix}", vehicle_entry=entry,
            vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
            status="DOCKED", total_boxes=Decimal(total_boxes),
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, dock, count):
        for index in range(count):
            SalesDispatchBoxScan.objects.create(
                company=self.oil, sales_dispatch=dock, box_barcode=f"BOX-{dock.id}-{index}",
                created_by=self.user, updated_by=self.user,
            )

    def _create_partial(self, dock, reason="Short load, rest to follow"):
        return self.client.post(
            "/api/v1/docking-admin/partial-scan-requests/",
            {"sales_dispatch": dock.id, "reason": reason},
            format="json",
            HTTP_COMPANY_CODE=self.oil.code,
        )

    def test_created_when_partially_scanned(self):
        dock = self._docking("601", total_boxes=10)
        self._scan(dock, 3)
        response = self._create_partial(dock)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["scanned_boxes"], 3)
        self.assertEqual(response.data["expected_boxes"], 10)
        self.assertEqual(response.data["status"], "PENDING")

    def test_rejected_when_nothing_scanned(self):
        dock = self._docking("602", total_boxes=10)
        self.assertEqual(self._create_partial(dock).status_code, 400)

    def test_rejected_when_fully_scanned(self):
        dock = self._docking("603", total_boxes=3)
        self._scan(dock, 3)
        self.assertEqual(self._create_partial(dock).status_code, 400)

    def test_approved_partial_satisfies_gatepass_box_scans(self):
        from gate_core.services.sales_dispatch_gatepass import get_gatepass_readiness

        dock = self._docking("604", total_boxes=10)
        self._scan(dock, 4)
        create = self._create_partial(dock)
        self.assertEqual(create.status_code, 201)
        # Pending partial scan -> the box-scan gate still blocks gatepass.
        self.assertIn("box_scans", get_gatepass_readiness(dock)["missing"])

        approve = self.client.post(
            f"/api/v1/docking-admin/partial-scan-requests/{create.data['id']}/approve/",
            {}, format="json", HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(approve.status_code, 200)
        # Approved -> box-scan requirement satisfied.
        self.assertNotIn("box_scans", get_gatepass_readiness(dock)["missing"])


@override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=[])
class PerBillScanCompletenessTests(TestCase):
    """The completeness gate must judge scanned-vs-invoiced per bill/line, so a surplus
    on one bill can't hide a shortfall on another, and weight-priced (KGS) lines the box
    estimate can't size are still accounted for."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="perbill@example.com",
            password="testpass123",
            full_name="Per Bill",
            employee_code="PB001",
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="docking_admin")
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-PB")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01PB0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Per Bill Driver", mobile_no="9000000003", license_no="DL-PB-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _docking(self, suffix):
        entry = VehicleEntry.objects.create(
            entry_no=f"PBV-{suffix}", company=self.oil, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.oil, entry_no=f"PBDOCK-{suffix}", vehicle_entry=entry,
            vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
            status="DOCKED", created_by=self.user, updated_by=self.user,
        )

    def _bill(self, entry, doc_entry, line_num, item_code, item_name, qty, sal_factor2=None):
        """One bill with a single line. ``sal_factor2`` is OITM.SalFactor2 -- the pieces
        per box SAP states. Left None (or 1) the item is not transacted in boxes and
        ships loose, exactly as the bill prints it."""
        doc = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=self.oil,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=doc_entry,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry, document=doc, line_num=line_num,
            item_code=item_code, item_name=item_name, quantity=Decimal(qty),
            sal_factor2=Decimal(sal_factor2) if sal_factor2 is not None else None,
        )
        return doc

    def _scan(self, entry, doc, item_code, count, qty_each):
        for index in range(count):
            SalesDispatchBoxScan.objects.create(
                company=self.oil, sales_dispatch=entry, document=doc,
                item_code=item_code, quantity=Decimal(qty_each),
                box_barcode=f"BOX-{doc.id}-{item_code}-{index}",
                created_by=self.user, updated_by=self.user,
            )

    def _create_partial(self, entry):
        return self.client.post(
            "/api/v1/docking-admin/partial-scan-requests/",
            {"sales_dispatch": entry.id, "reason": "Short, rest to follow"},
            format="json", HTTP_COMPANY_CODE=self.oil.code,
        )

    def test_over_scan_on_one_bill_does_not_mask_shortfall_on_another(self):
        from gate_core.services.sales_dispatch_gatepass import (
            get_gatepass_readiness, load_scan_status,
        )

        entry = self._docking("701")
        bill_a = self._bill(entry, 701001, 1, "FG1", "OIL 1 LTR 10 PCS", "100", "10")  # 10 boxes
        bill_b = self._bill(entry, 701002, 2, "FG2", "OIL 2 LTR 10 PCS", "100", "10")  # 10 boxes
        # Bill A under-scanned (5 boxes/50 PCS); Bill B over-scanned (15 boxes/150 PCS).
        self._scan(entry, bill_a, "FG1", 5, "10")
        self._scan(entry, bill_b, "FG2", 15, "10")

        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        # Load-wide totals net out to "complete" (20 == 20) — the old blind spot.
        self.assertEqual((scanned, expected), (20, 20))
        # ...but bill A is genuinely short, so the load is partial.
        self.assertTrue(is_partial)
        self.assertIn("box_scans", get_gatepass_readiness(entry)["missing"])
        # And the operator can actually raise the approval the gate demands (no deadlock).
        self.assertEqual(self._create_partial(entry).status_code, 201)

    def test_weight_item_counts_toward_expected_and_flags_shortfall(self):
        from gate_core.services.sales_dispatch_gatepass import (
            load_scan_status, resolved_expected_box_count, resolved_expected_loose_count,
        )

        entry = self._docking("702")
        self._bill(entry, 702001, 1, "FG3", "SOYABEAN OIL 12 KGS (B)", "38")  # SalFactor2 = 1
        # SAP transacts this tin per piece, so it has no box count -- the bill prints
        # "0 Box / 38 PCS". Its 38 invoiced pieces are gated on quantity instead.
        self.assertEqual(resolved_expected_box_count(entry), 0)
        self.assertEqual(resolved_expected_loose_count(entry), Decimal("38"))

        bill_pcs = self._bill(entry, 702002, 2, "FG4", "OIL 1 LTR 10 PCS", "100", "10")
        self._scan(entry, bill_pcs, "FG4", 10, "10")  # PCS bill fully scanned
        # The KGS bill has zero scans -> load is partial despite the other bill being full.
        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertTrue(is_partial)
        self.assertEqual(self._create_partial(entry).status_code, 201)

    def test_fully_scanned_per_bill_is_not_partial(self):
        from gate_core.services.sales_dispatch_gatepass import load_scan_status

        entry = self._docking("703")
        bill_a = self._bill(entry, 703001, 1, "FG5", "OIL 1 LTR 10 PCS", "100", "10")
        bill_b = self._bill(entry, 703002, 2, "FG6", "SOYABEAN OIL 13 KGS (B)", "20")
        self._scan(entry, bill_a, "FG5", 10, "10")  # 100 PCS == invoiced
        self._scan(entry, bill_b, "FG6", 20, "1")   # 20 units == invoiced
        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertFalse(is_partial)
        # Endpoint refuses an approval nobody needs.
        self.assertEqual(self._create_partial(entry).status_code, 400)

    def test_loose_line_has_no_box_count_to_lock_a_quantity_complete_load(self):
        """A line SAP transacts per piece (SalFactor2 = 1, non-CSD) carries no box count at
        all — the bill prints "0 Box / 18 PCS". The old rule invented one box per piece for
        it and then read a single 20-pc carton as 1 of 18, which is what locked fully
        loaded trucks. Completeness for these lines is judged on quantity."""
        from gate_core.services.sales_dispatch_gatepass import (
            load_scan_status, resolved_expected_box_count, resolved_expected_loose_count,
        )

        entry = self._docking("704")
        bill = self._bill(entry, 704001, 1, "FG7", "REFINED OIL 1000 MLS", "18")
        self.assertEqual(resolved_expected_box_count(entry), 0)
        self.assertEqual(resolved_expected_loose_count(entry), Decimal("18"))
        # One physical box carrying 20 pcs covers the 18 invoiced.
        self._scan(entry, bill, "FG7", 1, "20")

        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertEqual((scanned, expected), (1, 0))
        self.assertFalse(is_partial)  # quantity is complete
        # Nothing is held back, so the endpoint refuses a partial approval.
        self.assertEqual(self._create_partial(entry).status_code, 400)

    def test_quantity_shortfall_still_flags_partial_when_box_count_looks_full(self):
        """The exact-quantity path must still CATCH a genuine shortfall — a line under its
        invoiced quantity is partial however many boxes were scanned against it."""
        from gate_core.services.sales_dispatch_gatepass import load_scan_status

        entry = self._docking("705")
        # SalFactor2 = 1 and not CSD -> ships loose, so there is no box count to meet.
        bill = self._bill(entry, 705001, 1, "FG8", "REFINED OIL 2 LTR", "10")
        # 10 boxes scanned looks like plenty, but they carry only 8 pcs in total.
        self._scan(entry, bill, "FG8", 10, "0.8")

        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertEqual((scanned, expected), (10, 0))  # box count can't judge this...
        self.assertTrue(is_partial)                     # ...but 8 < 10 invoiced qty.
        self.assertEqual(self._create_partial(entry).status_code, 201)
