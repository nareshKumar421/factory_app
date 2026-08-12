"""HTTP-level (DRF APIClient) tests for the marketplace API.

The rest of the suite is service-level; this module exercises the actual
endpoints through the URL router + permission stack, to catch 500s, auth/tenant
gating regressions, and wiring bugs the service tests can't see.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from .models import (
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceScan,
    MarketplaceWarehouse,
    SkuMapping,
    SkuType,
)

BASE = "/api/v1/marketplace"
CH = MarketplaceChannel.FLIPKART

# Read endpoints that hit the DB only (no SAP/HANA), each safe with ?channel.
GET_ENDPOINTS = [
    "/settings/",
    "/warehouses/",
    "/sku-mappings/",
    "/combos/",
    "/orders/",
    "/batches/",
    "/issue-requests/",
    "/packing/queue/",
    "/packing/summary/",
    "/dispatches/sheets/",
    "/delivery-notes/sheets/",
    "/returns/",
    "/reconciliation/",
]


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class MarketplaceApiSmokeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="MP Ops")
        User = get_user_model()
        # Superuser so has_perm() passes for every marketplace codename.
        cls.user = User.objects.create_superuser(
            email="mp@example.com", password="x", full_name="MP", employee_code="MP1",
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True,
        )
        # A user with NO marketplace permissions (for the 403 permission check).
        cls.noperm = User.objects.create_user(
            email="noperm@example.com", password="x", full_name="No", employee_code="NP1",
        )
        UserCompany.objects.create(
            user=cls.noperm, company=cls.company, role=role, is_default=True, is_active=True,
        )

    def client_as(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        return c

    # ── auth / tenant gating ─────────────────────────────────────────────────
    def test_unauthenticated_is_rejected(self):
        resp = APIClient().get(f"{BASE}/warehouses/", HTTP_COMPANY_CODE="JIVO_MART")
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_missing_company_code_header_is_rejected(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        resp = c.get(f"{BASE}/warehouses/")  # no Company-Code header
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_permission_required_for_write(self):
        c = self.client_as(self.noperm)
        resp = c.post(f"{BASE}/warehouses/", {"channel": CH, "name": "X"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ── read endpoints must not 500 ──────────────────────────────────────────
    def test_get_endpoints_do_not_error(self):
        c = self.client_as(self.user)
        failures = []
        for path in GET_ENDPOINTS:
            resp = c.get(f"{BASE}{path}?channel={CH}")
            if resp.status_code >= 500:
                failures.append(f"{path} -> {resp.status_code}")
            self.assertLess(resp.status_code, 500, f"{path} returned {resp.status_code}")
        self.assertEqual(failures, [])

    def test_masters_list_ok(self):
        c = self.client_as(self.user)
        for path in ("/warehouses/", "/sku-mappings/", "/combos/"):
            resp = c.get(f"{BASE}{path}")
            self.assertEqual(resp.status_code, status.HTTP_200_OK, path)

    def test_reconcile_get_requires_view_permission(self):
        # A1 regression: the GET must be permission-gated, not open to any company user.
        denied = self.client_as(self.noperm).get(f"{BASE}/delivery-notes/reconcile/?channel={CH}")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        allowed = self.client_as(self.user).get(f"{BASE}/delivery-notes/reconcile/?channel={CH}")
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)

    def test_posted_delivery_notes_bad_limit_does_not_500(self):
        # A2 regression: a non-numeric ?limit must not raise ValueError -> 500.
        resp = self.client_as(self.user).get(f"{BASE}/delivery-notes/posted/?channel={CH}&limit=abc")
        self.assertLess(resp.status_code, 500)

    def test_orders_endpoint_is_paginated(self):
        # A4: orders/ now returns the shared pagination envelope, not a bare list.
        resp = self.client_as(self.user).get(f"{BASE}/orders/?channel={CH}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIn("results", body)
        self.assertIn("count", body)
        self.assertIsInstance(body["results"], list)

    def test_cut_rejects_bad_dispatch_ids_with_400(self):
        # A3: non-integer dispatch_ids must be a 400 (serializer), not a 500.
        resp = self.client_as(self.user).post(
            f"{BASE}/delivery-notes/cut/?channel={CH}",
            {"dispatch_ids": ["not-an-int"]}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    # ── round-trip write: create + read a warehouse master ───────────────────
    def test_create_and_list_warehouse(self):
        c = self.client_as(self.user)
        payload = {
            "channel": CH, "name": "Main", "sap_warehouse_code": "WH1",
            "sap_customer_card_code": "C-FLIP",
        }
        created = c.post(f"{BASE}/warehouses/", payload, format="json")
        self.assertIn(created.status_code, (200, 201), created.content)
        listed = c.get(f"{BASE}/warehouses/")
        self.assertEqual(listed.status_code, 200)

    def test_only_one_default_warehouse_per_channel(self):
        # _enforce_single_default: creating a second default demotes the first.
        c = self.client_as(self.user)
        a = c.post(f"{BASE}/warehouses/", {
            "channel": CH, "name": "A", "sap_warehouse_code": "WHA",
            "sap_customer_card_code": "CA", "is_default": True,
        }, format="json")
        b = c.post(f"{BASE}/warehouses/", {
            "channel": CH, "name": "B", "sap_warehouse_code": "WHB",
            "sap_customer_card_code": "CB", "is_default": True,
        }, format="json")
        self.assertIn(a.status_code, (200, 201))
        self.assertIn(b.status_code, (200, 201))
        defaults = [w for w in c.get(f"{BASE}/warehouses/?channel={CH}").json() if w["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["name"], "B")

    def test_sku_mapping_import_upserts_rows(self):
        c = self.client_as(self.user)
        body = {"rows": [{
            "channel": CH, "marketplace_sku": "IMPORTSKU", "sku_type": SkuType.RAW,
            "fg_item_code": "FG-IMP", "fg_item_name": "Imported FG",
        }]}
        resp = c.post(f"{BASE}/sku-mappings/import/", body, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["imported"], 1)
        self.assertTrue(SkuMapping.objects.filter(
            company=self.company, marketplace_sku="IMPORTSKU").exists())


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class MarketplaceConfirmGateApiTests(APITestCase):
    """The full-scan confirm gate must surface as HTTP 409 NOT_SCANNED."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="MP Ops")
        self.user = get_user_model().objects.create_superuser(
            email="gate@example.com", password="x", full_name="Gate", employee_code="G1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_default=True, is_active=True,
        )
        MarketplaceWarehouse.objects.create(
            company=self.company, channel=CH, name="Main",
            sap_warehouse_code="WH1", sap_customer_card_code="C-FLIP",
        )
        SkuMapping.objects.create(
            company=self.company, channel=CH, marketplace_sku="TESTSKU",
            sku_type=SkuType.RAW, fg_item_code="FG-T", fg_item_name="FG T",
        )
        # A non-sheet order (no import_batch → dispatch-ready) with a Tracking ID.
        self.order = MarketplaceOrder.objects.create(
            company=self.company, channel=CH, order_id="APIORD", buyer_name="B",
        )
        MarketplaceOrderLine.objects.create(
            order=self.order, marketplace_sku="TESTSKU", ordered_quantity=Decimal("1"),
            tracking_id="TRK-API",
        )
        self.dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=CH, order=self.order,
            status=MarketplaceDispatchStatus.READY,
        )

    def client_ok(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        return c

    def test_confirm_without_scan_returns_409_not_scanned(self):
        c = self.client_ok()
        resp = c.post(f"{BASE}/dispatches/{self.dispatch.id}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertEqual(resp.json().get("code"), "NOT_SCANNED")
        self.dispatch.refresh_from_db()
        self.assertEqual(self.dispatch.status, MarketplaceDispatchStatus.READY)  # unchanged

    def test_confirm_with_override_succeeds(self):
        c = self.client_ok()
        resp = c.post(
            f"{BASE}/dispatches/{self.dispatch.id}/confirm/",
            {"override_deviation": True, "remarks": "damaged label"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.dispatch.refresh_from_db()
        self.assertEqual(self.dispatch.status, MarketplaceDispatchStatus.CONFIRMED)

    def test_cancel_dispatch(self):
        c = self.client_ok()
        resp = c.post(f"{BASE}/dispatches/{self.dispatch.id}/cancel/",
                      {"reason": "customer cancelled"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.dispatch.refresh_from_db()
        self.assertEqual(self.dispatch.status, MarketplaceDispatchStatus.CANCELLED)

    def test_delete_scan_recomputes_status(self):
        # A dispatch with one scan → deleting it drops back to DRAFT (no scans left).
        scan = MarketplaceScan.objects.create(
            company=self.company, dispatch=self.dispatch, barcode_raw="TRK-API#FG-T",
            item_code="FG-T", quantity=Decimal("1"),
        )
        c = self.client_ok()
        resp = c.delete(f"{BASE}/dispatches/{self.dispatch.id}/scans/{scan.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.dispatch.refresh_from_db()
        self.assertEqual(self.dispatch.status, MarketplaceDispatchStatus.DRAFT)
        self.assertFalse(self.dispatch.scans.exists())


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class MarketplaceDnExportApiTests(APITestCase):
    """The posted-DN CSV export endpoint (download headers + content)."""

    def setUp(self):
        from django.utils import timezone
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="MP Ops")
        self.user = get_user_model().objects.create_superuser(
            email="dn@example.com", password="x", full_name="DN", employee_code="DN1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_default=True, is_active=True,
        )
        MarketplaceWarehouse.objects.create(
            company=self.company, channel=CH, name="Main", sap_warehouse_code="WH1",
            sap_customer_card_code="C-FLIP", sap_branch_id=1, is_default=True,
        )
        SkuMapping.objects.create(
            company=self.company, channel=CH, marketplace_sku="TESTSKU",
            sku_type=SkuType.RAW, fg_item_code="FG-T", fg_item_name="FG T",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=CH, order_id="DNORD", buyer_name="B",
            sap_warehouse_code="WH1",
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="TESTSKU", ordered_quantity=Decimal("2"),
            hsn_code="15099090", invoice_amount="500",
        )
        MarketplaceDispatch.objects.create(
            company=self.company, channel=CH, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_delivery_note_doc_entry=9001, sap_delivery_note_num="DN9001",
            confirmed_at=timezone.now(),
        )

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        return c

    def test_export_downloads_csv(self):
        resp = self._client().get(f"{BASE}/delivery-notes/9001/export.csv?channel={CH}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("text/csv", resp["Content-Type"])
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("DN9001", resp["Content-Disposition"])
        body = resp.content.decode()
        self.assertIn("DN Number", body)     # header row
        self.assertIn("FG-T", body)          # item code
        self.assertIn("15099090", body)      # HSN
        self.assertIn("DNORD", body)         # order id

    def test_export_missing_dn_is_404(self):
        resp = self._client().get(f"{BASE}/delivery-notes/999999/export.csv")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_export_requires_auth(self):
        resp = APIClient().get(f"{BASE}/delivery-notes/9001/export.csv", HTTP_COMPANY_CODE="JIVO_MART")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class MarketplaceGateApiTests(APITestCase):
    """The Gate page works through the URL router + permission stack for a user whose
    ONLY marketplace permission is gate_check (granted via the gate_core group)."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group, Permission

        from .models import OrderImportBatch
        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Gate")
        User = get_user_model()
        # Gate user with ONLY marketplace.gate_check, via the gate_core group.
        cls.gate_user = User.objects.create_user(
            email="gate@x.com", password="x", full_name="Gate", employee_code="GA1")
        UserCompany.objects.create(
            user=cls.gate_user, company=cls.company, role=role, is_default=True, is_active=True)
        gate_group, _ = Group.objects.get_or_create(name="gate_core")
        gate_group.permissions.add(
            Permission.objects.get(content_type__app_label="marketplace", codename="gate_check"))
        cls.gate_user.groups.add(gate_group)
        # A user with no marketplace permission at all.
        cls.noperm = User.objects.create_user(
            email="np@x.com", password="x", full_name="NP", employee_code="NP9")
        UserCompany.objects.create(
            user=cls.noperm, company=cls.company, role=role, is_default=True, is_active=True)
        # One confirmed order on a sheet — ready at the gate.
        cls.batch = OrderImportBatch.objects.create(company=cls.company, channel=CH, filename="g.csv")
        o = MarketplaceOrder.objects.create(
            company=cls.company, channel=CH, order_id="OGA1", import_batch=cls.batch,
            buyer_name="Buyer", city="Delhi", state="Delhi")
        MarketplaceOrderLine.objects.create(
            order=o, marketplace_sku="S", sku_name="Item", ordered_quantity=1, tracking_id="TGA1")
        MarketplaceDispatch.objects.create(
            company=cls.company, channel=CH, order=o,
            status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_num="DN1")

    def client_as(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        return c

    def test_gate_user_can_view_detail_and_approve(self):
        c = self.client_as(self.gate_user)
        q = c.get(f"{BASE}/gate/queue/?channel={CH}")
        self.assertEqual(q.status_code, status.HTTP_200_OK)
        self.assertEqual(q.json()["total_sheets"], 1)
        self.assertEqual(q.json()["total_parcels"], 1)

        d = c.get(f"{BASE}/gate/{self.batch.id}/?channel={CH}")
        self.assertEqual(d.status_code, status.HTTP_200_OK)
        self.assertEqual(d.json()["total_orders"], 1)

        a = c.post(f"{BASE}/gate/{self.batch.id}/approve/?channel={CH}", {}, format="json")
        self.assertEqual(a.status_code, status.HTTP_200_OK)
        self.assertEqual(a.json()["approved"], 1)

    def test_gate_hold_records_remark(self):
        c = self.client_as(self.gate_user)
        r = c.post(f"{BASE}/gate/{self.batch.id}/hold/?channel={CH}", {"remarks": "damaged box"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["held"], 1)

    def test_user_without_gate_check_is_forbidden(self):
        c = self.client_as(self.noperm)
        self.assertEqual(c.get(f"{BASE}/gate/queue/?channel={CH}").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            c.post(f"{BASE}/gate/{self.batch.id}/approve/?channel={CH}", {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN)


class GateCheckMigrationTest(APITestCase):
    """Migration 0029's grant() adds marketplace.gate_check to the Marketplace AND
    gate_core groups (validated against the schema built from the models)."""

    def test_grant_adds_gate_check_to_both_groups(self):
        import importlib

        from django.apps import apps as real_apps
        from django.contrib.auth.models import Group

        mod = importlib.import_module("marketplace.migrations.0029_gate_check_group_perms")
        mod.grant(real_apps, None)  # idempotent
        for name in ["Marketplace", "gate_core"]:
            g = Group.objects.get(name=name)
            self.assertTrue(
                g.permissions.filter(
                    codename="gate_check", content_type__app_label="marketplace"
                ).exists(),
                f"{name} should have gate_check",
            )
        # Idempotent — running again doesn't duplicate.
        mod.grant(real_apps, None)
        self.assertEqual(
            Group.objects.get(name="gate_core").permissions.filter(codename="gate_check").count(), 1)


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class GatePassApiTests(APITestCase):
    """The outward trip over HTTP: open, weigh, print, out.

    Service rules are covered in tests_sheet_flow.GatePassTests; this drives the
    same ladder through the URL router and permission stack.
    """

    @classmethod
    def setUpTestData(cls):
        from driver_management.models import Driver
        from vehicle_management.models import Transporter, Vehicle, VehicleType

        from .models import MarketplaceGateStatus, OrderImportBatch

        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="MP Gate")
        User = get_user_model()
        cls.user = User.objects.create_superuser(
            email="gpapi@example.com", password="x", full_name="GP", employee_code="GPA1",
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True)
        cls.noperm = User.objects.create_user(
            email="gpnone@example.com", password="x", full_name="No", employee_code="GPA2",
        )
        UserCompany.objects.create(
            user=cls.noperm, company=cls.company, role=role, is_default=True, is_active=True)

        vt = VehicleType.objects.create(name="TEMPO-API")
        cls.transporter = Transporter.objects.create(name="Arnav Transport")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="DL01API001", vehicle_type=vt, transporter=cls.transporter)
        cls.driver = Driver.objects.create(
            name="Soyab", mobile_no="9000000001", license_no="DL-API-1")

        cls.batch = OrderImportBatch.objects.create(
            company=cls.company, channel=CH, filename="gp-api.csv")
        order = MarketplaceOrder.objects.create(
            company=cls.company, channel=CH, order_id="GPA-1",
            import_batch=cls.batch, buyer_name="Buyer", city="Delhi", state="Delhi")
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="SKU", sku_name="Item",
            ordered_quantity=1, tracking_id="T-GPA-1")
        MarketplaceDispatch.objects.create(
            company=cls.company, channel=CH, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            gate_status=MarketplaceGateStatus.APPROVED, sap_delivery_note_num="DN1")

    def client_as(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        return c

    def _open(self):
        resp = self.client_as(self.user).post(
            f"{BASE}/gate-passes/?channel={CH}",
            {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id,
             "driver_id": self.driver.id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data

    def test_the_whole_ladder_open_weigh_print_out(self):
        c = self.client_as(self.user)
        gp = self._open()
        self.assertEqual(gp["status"], "DRAFT")
        self.assertEqual(gp["vehicle_no"], "DL01API001")
        # The transporter rides along from the vehicle.
        self.assertEqual(gp["transporter_name"], "Arnav Transport")

        r = c.post(f"{BASE}/gate-passes/{gp['id']}/weighment/",
                   {"tare_weight": "1000.000", "gross_weight": "1180.250",
                    "weighbridge_slip_no": "WB-9"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["net_weight"], "180.250")
        self.assertEqual(r.data["status"], "WEIGHED")
        self.assertEqual(r.data["weight_error"], "")

        r = c.post(f"{BASE}/gate-passes/{gp['id']}/print/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["gatepass_no"].startswith("MKT/JIVO_MART/"))

        r = c.post(f"{BASE}/gate-passes/{gp['id']}/dispatch/",
                   {"security_name": "Guard"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "DISPATCHED")
        self.assertEqual(r.data["parcel_count"], 1)
        self.assertEqual(
            MarketplaceDispatch.objects.filter(gate_pass_id=gp["id"]).count(), 1)

    def test_an_unweighed_trip_is_refused_with_the_reason(self):
        c = self.client_as(self.user)
        gp = self._open()
        c.post(f"{BASE}/gate-passes/{gp['id']}/print/", {}, format="json")
        r = c.post(f"{BASE}/gate-passes/{gp['id']}/dispatch/", {}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("Gross weight", str(r.data))

    def test_the_list_reports_why_a_trip_cannot_leave_yet(self):
        """So the screen can disable the button and say why in one read."""
        self._open()
        r = self.client_as(self.user).get(f"{BASE}/gate-passes/?channel={CH}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 1)
        self.assertIn("Gross weight", r.data[0]["weight_error"])
        self.assertFalse(r.data[0]["is_weighed"])

    def test_tare_over_gross_is_rejected(self):
        c = self.client_as(self.user)
        gp = self._open()
        r = c.post(f"{BASE}/gate-passes/{gp['id']}/weighment/",
                   {"tare_weight": "2000.000", "gross_weight": "1180.250"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_a_weighment_with_nothing_in_it_is_rejected(self):
        c = self.client_as(self.user)
        gp = self._open()
        r = c.post(f"{BASE}/gate-passes/{gp['id']}/weighment/", {}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_cancelling_needs_a_reason(self):
        c = self.client_as(self.user)
        gp = self._open()
        self.assertEqual(
            c.post(f"{BASE}/gate-passes/{gp['id']}/cancel/", {}, format="json").status_code, 400)
        r = c.post(f"{BASE}/gate-passes/{gp['id']}/cancel/",
                   {"reason": "vehicle broke down"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "CANCELLED")

    def test_a_user_without_the_permission_is_refused(self):
        c = self.client_as(self.noperm)
        self.assertEqual(c.get(f"{BASE}/gate-passes/?channel={CH}").status_code, 403)
        self.assertEqual(
            c.post(f"{BASE}/gate-passes/?channel={CH}",
                   {"batch_id": self.batch.id}, format="json").status_code, 403)
