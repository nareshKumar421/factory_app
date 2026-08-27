"""End-to-end gate-pass flow as the gate user, over HTTP.

Distinct from tests_api.GatePassApiTests, which drives the endpoints as a
superuser: here the user is an ordinary operator whose ONLY rights come from the
"marketplace gate" group, so the migration that grants them is part of what is
under test. A permission that exists but was never granted is the same as a
feature that was never shipped.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from .models import (
    MarketplaceChannel, MarketplaceDispatch, MarketplaceDispatchStatus,
    MarketplaceGateStatus, MarketplaceOrder, MarketplaceOrderLine, OrderImportBatch,
)

BASE = "/api/v1/marketplace"
CH = MarketplaceChannel.FLIPKART


def grant_perms(django_apps):
    """Run the real grant() of migrations 0029 and 0036 against the test database.

    Module names begin with a digit, so they are imported by string. Running the
    real functions means the migrations are what is tested — a hand-written copy
    here would pass even if a migration were wrong.

    Note 0029 grants gate_check to "Marketplace" and "gate_core" — NOT to
    "marketplace gate", which was created by hand in production. That is why 0036
    uses get_or_create on the group rather than assuming it exists.
    """
    import importlib

    importlib.import_module(
        "marketplace.migrations.0036_gate_pass_group_perms").grant(django_apps, None)


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class GatePassFlowAsGateUserTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        from driver_management.models import Driver
        from vehicle_management.models import Transporter, Vehicle, VehicleType

        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Gate")
        User = get_user_model()

        # An ordinary user — no superuser shortcut. Rights come from the group
        # the migration grants to, exactly as jasmeet@jivo.in gets them.
        cls.user = User.objects.create_user(
            email="gate@jivo.in", password="x", full_name="Gate Person",
            employee_code="GATE1",
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True)
        # The test database is built with migrations disabled, so the data
        # migration has not run. Call its own grant() rather than duplicating the
        # grant here — that keeps the migration itself under test.
        from django.apps import apps as django_apps

        # Mirror production: the group already holds gate_check, granted by hand.
        # Seeding it BEFORE the migration is what makes "adds, not replaces"
        # testable — a careless .set() would strip it.
        from django.contrib.auth.models import Permission

        seeded, _ = Group.objects.get_or_create(name="marketplace gate")
        existing = Permission.objects.filter(
            codename="gate_check", content_type__app_label="marketplace").first()
        if existing:
            seeded.permissions.add(existing)

        grant_perms(django_apps)

        group = Group.objects.get(name="marketplace gate")
        cls.user.groups.add(group)
        cls.group = group

        vt = VehicleType.objects.create(name="TEMPO-FLOW")
        cls.transporter = Transporter.objects.create(
            name="Arnav Transport Service", gstin="07AAACA1111A1Z5")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LAT2433", vehicle_type=vt, transporter=cls.transporter)
        cls.driver = Driver.objects.create(
            name="Soyab", mobile_no="9671747754", license_no="DL-0420110149646")

        cls.batch = OrderImportBatch.objects.create(
            company=cls.company, channel=CH, filename="Order-CSV-flow.csv")
        for i in range(3):
            order = MarketplaceOrder.objects.create(
                company=cls.company, channel=CH, order_id=f"OD-FLOW-{i}",
                import_batch=cls.batch, buyer_name=f"Buyer {i}", city="Bengaluru", state="KA")
            MarketplaceOrderLine.objects.create(
                order=order, marketplace_sku="SKU", sku_name="Olive Oil 1 L",
                ordered_quantity=2, tracking_id=f"FMPC6351293{i}")
            MarketplaceDispatch.objects.create(
                company=cls.company, channel=CH, order=order,
                status=MarketplaceDispatchStatus.CONFIRMED,
                gate_status=MarketplaceGateStatus.APPROVED,
                sap_delivery_note_num="1508264508")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_MART")

    # ─── the grant itself ─────────────────────────────────────────────────

    def test_the_group_carries_every_gate_pass_permission(self):
        held = set(self.group.permissions.values_list("codename", flat=True))
        for codename in (
            "can_view_mp_gate_pass", "can_manage_mp_gate_pass",
            "can_weigh_mp_gate_pass", "can_print_mp_gate_pass",
            "can_dispatch_mp_gate_pass",
        ):
            self.assertIn(codename, held, f"{codename} was never granted")
        # 0036 must ADD to the group, not replace what was already there.
        self.assertIn("gate_check", held)

    def test_a_user_outside_the_group_still_cannot_see_trips(self):
        User = get_user_model()
        outsider = User.objects.create_user(
            email="nobody@jivo.in", password="x", full_name="No", employee_code="NO1")
        UserCompany.objects.create(
            user=outsider, company=self.company,
            role=UserRole.objects.create(name="Other"), is_default=True, is_active=True)
        c = APIClient()
        c.force_authenticate(outsider)
        c.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        self.assertEqual(c.get(f"{BASE}/gate-passes/?channel={CH}").status_code, 403)

    # ─── the flow ─────────────────────────────────────────────────────────

    def test_the_gate_user_can_run_the_whole_trip(self):
        c = self.client

        # 1. open the trip
        r = c.post(f"{BASE}/gate-passes/?channel={CH}",
                   {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id,
                    "driver_id": self.driver.id}, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        trip = r.data["id"]
        self.assertEqual(r.data["vehicle_no"], "DL01LAT2433")
        # The transporter rides along from the vehicle, not typed.
        self.assertEqual(r.data["transporter_name"], "Arnav Transport Service")
        self.assertEqual(r.data["driver_mobile_no"], "9671747754")
        self.assertEqual(r.data["status"], "DRAFT")

        # 2. weigh empty — half a weighment is not a net weight
        r = c.post(f"{BASE}/gate-passes/{trip}/weighment/",
                   {"tare_weight": "2450.000"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.data["net_weight"])
        self.assertEqual(r.data["status"], "DRAFT")
        self.assertIn("Gross weight", r.data["weight_error"])

        # 3. weigh loaded
        r = c.post(f"{BASE}/gate-passes/{trip}/weighment/",
                   {"gross_weight": "2712.500", "weighbridge_slip_no": "WB-88213"},
                   format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["net_weight"], "262.500")
        self.assertEqual(r.data["status"], "WEIGHED")
        self.assertEqual(r.data["weight_error"], "")

        # 4. out — no separate print step gates it
        r = c.post(f"{BASE}/gate-passes/{trip}/dispatch/",
                   {"security_name": "Rakesh"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "DISPATCHED")
        self.assertEqual(r.data["order_count"], 3)
        self.assertEqual(r.data["parcel_count"], 3)
        self.assertEqual(r.data["security_name"], "Rakesh")
        self.assertIsNotNone(r.data["out_time"])
        gatepass_no = r.data["gatepass_no"]
        self.assertTrue(gatepass_no.startswith("MKT/JIVO_MART/"))

        # 5. the pass the driver carries can be printed after the truck has gone
        r = c.post(f"{BASE}/gate-passes/{trip}/print/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["gatepass_no"], gatepass_no)
        self.assertEqual(r.data["status"], "DISPATCHED")

        # 6. the parcels are stamped, so nothing can ride a second trip
        self.assertEqual(
            MarketplaceDispatch.objects.filter(gate_pass_id=trip).count(), 3)
        r = c.post(f"{BASE}/gate-passes/?channel={CH}",
                   {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id},
                   format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("gate-approved", str(r.data).lower())

    def test_the_list_shows_the_finished_trip_with_its_load(self):
        c = self.client
        trip = c.post(f"{BASE}/gate-passes/?channel={CH}",
                      {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id},
                      format="json").data["id"]
        c.post(f"{BASE}/gate-passes/{trip}/weighment/",
               {"tare_weight": "2450.000", "gross_weight": "2712.500"}, format="json")
        c.post(f"{BASE}/gate-passes/{trip}/dispatch/", {"security_name": "Rakesh"},
               format="json")

        r = c.get(f"{BASE}/gate-passes/?channel={CH}&status=DISPATCHED")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 1)
        row = r.data[0]
        self.assertEqual(row["net_weight"], "262.500")
        self.assertEqual(row["parcel_count"], 3)
        self.assertEqual(row["weight_error"], "")

    def test_a_cancelled_trip_releases_its_parcels(self):
        c = self.client
        trip = c.post(f"{BASE}/gate-passes/?channel={CH}",
                      {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id},
                      format="json").data["id"]
        r = c.post(f"{BASE}/gate-passes/{trip}/cancel/",
                   {"reason": "vehicle broke down"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "CANCELLED")
        # Nothing was stamped, so the next trip can carry them.
        r = c.post(f"{BASE}/gate-passes/?channel={CH}",
                   {"batch_id": self.batch.id, "vehicle_id": self.vehicle.id},
                   format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class GatePassAttachmentTests(APITestCase):
    """The papers that travel with the trip.

    A trip used to record only vehicle, weight and time — the SAP delivery note
    the driver carried lived in someone's Downloads folder, so nothing could be
    reconciled against it afterwards.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Gate")
        User = get_user_model()
        cls.user = User.objects.create_user(
            email="gate2@jivo.in", password="x", full_name="Gate Person",
            employee_code="GATE2", is_superuser=True, is_staff=True,
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True)

        cls.batch = OrderImportBatch.objects.create(
            company=cls.company, channel=CH, filename="Order-CSV-docs.csv")
        order = MarketplaceOrder.objects.create(
            company=cls.company, channel=CH, order_id="OD-DOC-1",
            import_batch=cls.batch, buyer_name="Buyer", city="Delhi", state="DL")
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="SKU", sku_name="Pomace Olive 1 LTR",
            ordered_quantity=1, tracking_id="FMPC63512999")
        MarketplaceDispatch.objects.create(
            company=cls.company, channel=CH, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            gate_status=MarketplaceGateStatus.APPROVED)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_MART")
        self.trip = self.client.post(
            f"{BASE}/gate-passes/?channel={CH}", {"batch_id": self.batch.id},
            format="json",
        ).data["id"]

    def _note(self, name="1508264519.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, b"%PDF-1.4 delivery note", content_type="application/pdf")

    def _upload(self, **extra):
        payload = {"file": self._note(), "document_type": "DELIVERY_NOTE"}
        payload.update(extra)
        return self.client.post(
            f"{BASE}/gate-passes/{self.trip}/attachments/", payload, format="multipart")

    def test_a_delivery_note_hangs_on_the_trip(self):
        r = self._upload(document_no="1508264519", document_date="2026-08-27")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.assertEqual(r.data["document_type"], "DELIVERY_NOTE")
        self.assertEqual(r.data["document_no"], "1508264519")
        self.assertEqual(r.data["original_filename"], "1508264519.pdf")
        self.assertEqual(r.data["uploaded_by_name"], "Gate Person")
        self.assertTrue(r.data["file_url"])

        # and the trip itself carries it, so the screen needs no second call
        r = self.client.get(f"{BASE}/gate-passes/{self.trip}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data["attachments"]), 1)
        self.assertEqual(r.data["attachments"][0]["document_no"], "1508264519")

    def test_the_paperwork_may_arrive_after_the_truck_has_gone(self):
        """The signed note and the bilty come back to the office hours later."""
        self.client.post(f"{BASE}/gate-passes/{self.trip}/weighment/",
                         {"tare_weight": "2450.000", "gross_weight": "2712.500"},
                         format="json")
        r = self.client.post(f"{BASE}/gate-passes/{self.trip}/dispatch/", {}, format="json")
        self.assertEqual(r.data["status"], "DISPATCHED")

        r = self._upload(document_type="BILTY", document_no="LR-9912")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)

    def test_a_cancelled_trip_takes_no_documents(self):
        self.client.post(f"{BASE}/gate-passes/{self.trip}/cancel/",
                         {"reason": "vehicle broke down"}, format="json")
        r = self._upload()
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data.get("code"), "CANCELLED")

    def test_a_wrong_upload_is_removed_from_the_list_but_kept_on_file(self):
        from .models import MarketplaceGatePassAttachment

        attachment_id = self._upload().data["id"]
        r = self.client.delete(
            f"{BASE}/gate-passes/{self.trip}/attachments/{attachment_id}/")
        self.assertEqual(r.status_code, 204, r.content)

        r = self.client.get(f"{BASE}/gate-passes/{self.trip}/attachments/")
        self.assertEqual(r.data, [])
        # The row survives, so the audit trail can still surface it.
        removed = MarketplaceGatePassAttachment.objects.get(id=attachment_id)
        self.assertFalse(removed.is_active)
        self.assertEqual(removed.removed_by, self.user)

    def test_another_companys_trip_is_not_reachable(self):
        other = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        UserCompany.objects.create(
            user=self.user, company=other,
            role=UserRole.objects.create(name="Gate2"), is_active=True)
        c = APIClient()
        c.force_authenticate(self.user)
        c.credentials(HTTP_COMPANY_CODE="JIVO_OIL")
        r = c.get(f"{BASE}/gate-passes/{self.trip}/attachments/")
        # The marketplace module is bound to one company unit; a trip raised
        # under Mart must not be readable from another company's context.
        self.assertIn(r.status_code, (403, 404))


@override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART", MARKETPLACE_SIMULATE_SAP=True)
class ManualGateOutTests(APITestCase):
    """A truck leaving on a delivery note alone, with no sheet behind it.

    Before this, the only way out was the full ladder — scan, confirm,
    gate-approve, then a trip — and a load cut by hand in SAP could not use it.
    The truck either waited or left unrecorded.
    """

    @classmethod
    def setUpTestData(cls):
        from driver_management.models import Driver
        from vehicle_management.models import Transporter, Vehicle, VehicleType

        cls.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Gate")
        User = get_user_model()
        cls.user = User.objects.create_user(
            email="gate3@jivo.in", password="x", full_name="Gate Person",
            employee_code="GATE3", is_superuser=True, is_staff=True,
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True)

        vt = VehicleType.objects.create(name="TEMPO")
        cls.transporter = Transporter.objects.create(name="Arnav Transport")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="HR55AK6402", vehicle_type=vt, transporter=cls.transporter)
        cls.driver = Driver.objects.create(
            name="Soyab", mobile_no="9671747754", license_no="DL-042")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_MART")

    def _note(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(
            "1508264519.pdf", b"%PDF-1.4 note", content_type="application/pdf")

    def test_the_gate_person_opens_and_sends_a_truck_in_one_go(self):
        r = self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {
                "vehicle_id": self.vehicle.id, "driver_id": self.driver.id,
                "delivery_note_no": "1508264519", "delivery_note_date": "2026-08-27",
                "box_count": 62, "security_name": "Rakesh",
                "file": self._note(),
            },
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.assertEqual(r.data["status"], "DISPATCHED")
        self.assertTrue(r.data["is_manual"])
        self.assertEqual(r.data["sheet"], "")
        self.assertIsNone(r.data["import_batch"])
        # The load is what the note says, since there are no parcels to count.
        self.assertEqual(r.data["parcel_count"], 62)
        self.assertEqual(r.data["order_count"], 0)
        self.assertEqual(r.data["delivery_note_no"], "1508264519")
        self.assertEqual(r.data["vehicle_no"], "HR55AK6402")
        self.assertEqual(r.data["driver_mobile_no"], "9671747754")
        self.assertTrue(r.data["gatepass_no"].startswith("MKT/JIVO_MART/"))
        self.assertIsNotNone(r.data["out_time"])
        # The note travelled with it.
        self.assertEqual(len(r.data["attachments"]), 1)
        self.assertEqual(r.data["attachments"][0]["document_type"], "DELIVERY_NOTE")
        self.assertEqual(r.data["attachments"][0]["document_no"], "1508264519")

    def test_no_weighbridge_reading_does_not_hold_the_truck(self):
        """The exemption that makes this entry point worth having."""
        r = self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {"vehicle_no": "HR38AB1883", "delivery_note_no": "1508264520", "box_count": 4},
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.assertEqual(r.data["status"], "DISPATCHED")
        self.assertEqual(r.data["weight_error"], "")
        self.assertIsNone(r.data["net_weight"])
        # An unregistered truck is recorded from what was typed.
        self.assertEqual(r.data["vehicle_no"], "HR38AB1883")

    def test_a_weighed_manual_trip_still_rejects_a_mis_keyed_weighment(self):
        r = self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {"vehicle_no": "HR38AB1883", "tare_weight": "2900.000",
             "gross_weight": "2450.000"},
            format="multipart",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("tare", str(r.data).lower())

    def test_a_truck_with_no_number_is_refused(self):
        r = self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {"delivery_note_no": "1508264519"}, format="multipart")
        self.assertEqual(r.status_code, 400, r.content)

    def test_it_can_be_left_in_draft_to_mark_out_later(self):
        r = self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {"vehicle_id": self.vehicle.id, "box_count": 10, "mark_out": False},
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.assertEqual(r.data["status"], "DRAFT")
        self.assertIsNone(r.data["gatepass_no"])

        r = self.client.post(
            f"{BASE}/gate-passes/{r.data['id']}/dispatch/", {"security_name": "Rakesh"},
            format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "DISPATCHED")
        self.assertEqual(r.data["parcel_count"], 10)

    def test_a_manual_trip_shows_up_in_the_list(self):
        self.client.post(
            f"{BASE}/gate-passes/manual/?channel={CH}",
            {"vehicle_id": self.vehicle.id, "box_count": 62}, format="multipart")
        r = self.client.get(f"{BASE}/gate-passes/?channel={CH}&status=DISPATCHED")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 1)
        self.assertTrue(r.data[0]["is_manual"])
        self.assertEqual(r.data[0]["sheet"], "")
