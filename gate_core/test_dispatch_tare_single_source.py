"""Tare must be single-sourced from the physical truck's arrival, not each
company weighment's independently-entered (drift-prone) copy.

A multi-company truck stores the same empty weight on the arrival, on every
per-company gate-in weighment, and again on the docking weighment; operators edit
those copies independently and they drift (seen live: one truck 5000 kg vs 700 kg).
The dispatch weight check and the tare/net display now read the arrival's tare when
the docking belongs to one, so every chain reconciles against the same empty weight.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
    VehicleArrival,
)
from gate_core.serializers_sales_dispatch import SalesDispatchGateOutSerializer
from gate_core.services.sales_dispatch_dispatch import (
    dispatch_tare_weight,
    get_dispatch_weight_error,
)
from vehicle_management.models import Transporter, Vehicle
from weighment.models import Weighment


class DispatchTareSingleSourceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create_user(
            email="tare@example.com", password="p", full_name="Tare", employee_code="TR1",
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01MA6176", transporter=self.transporter)
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")

    _doc_seq = 0

    def _docking(self, *, arrival=None, gross, docking_tare):
        DispatchTareSingleSourceTests._doc_seq += 1
        seq = DispatchTareSingleSourceTests._doc_seq
        ve = VehicleEntry.objects.create(
            entry_no=f"DOCKV-{seq}", company=self.company,
            vehicle=self.vehicle, driver=self.driver, entry_type="SALES_DISPATCH",
            status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=ve, gross_weight=Decimal(gross), tare_weight=Decimal(docking_tare),
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no=f"DOCK-{seq}", vehicle_entry=ve, arrival=arrival,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=seq, sap_doc_num=str(seq),
            status=SalesDispatchGateOutStatus.PRINT_COMMITTED, created_by=self.user, updated_by=self.user,
        )

    def _arrival(self, tare):
        return VehicleArrival.objects.create(
            arrival_no=f"ARV-{timezone.now().timestamp()}", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            tare_weight=Decimal(tare), created_by=self.user, updated_by=self.user,
        )

    def test_arrival_tare_overrides_drifted_docking_weighment(self):
        arrival = self._arrival("1000.000")
        # Docking weighment drifted to 700; arrival (truth) is 1000.
        entry = self._docking(arrival=arrival, gross="900.000", docking_tare="700.000")
        self.assertEqual(dispatch_tare_weight(entry), Decimal("1000.000"))

    def test_weight_gate_uses_arrival_tare(self):
        # The gate reconciles the loaded gross against the single arrival tare (the
        # weighbridge truth at the gate), not the docking's editable/drift-prone copy.
        arrival = self._arrival("1000.000")
        # Drifted docking tare (700) alone would pass; the real arrival tare (1000) > gross (900).
        entry = self._docking(arrival=arrival, gross="900.000", docking_tare="700.000")
        self.assertIn("Tare weight cannot be greater", get_dispatch_weight_error(entry))
        # A gross above the arrival tare dispatches clean.
        entry2 = self._docking(arrival=arrival, gross="1500.000", docking_tare="700.000")
        self.assertEqual(get_dispatch_weight_error(entry2), "")

    def test_legacy_docking_without_arrival_uses_its_weighment_tare(self):
        entry = self._docking(arrival=None, gross="900.000", docking_tare="250.000")
        self.assertEqual(dispatch_tare_weight(entry), Decimal("250.000"))
        self.assertEqual(get_dispatch_weight_error(entry), "")

    def test_serializer_tare_and_net_use_canonical_tare(self):
        arrival = self._arrival("1000.000")
        entry = self._docking(arrival=arrival, gross="1500.000", docking_tare="700.000")
        data = SalesDispatchGateOutSerializer(entry).data
        self.assertEqual(Decimal(data["tare_weight"]), Decimal("1000.000"))
        self.assertEqual(Decimal(data["net_weight"]), Decimal("500.000"))  # 1500 - 1000, not 1500 - 700

    def test_zero_arrival_tare_falls_back_to_docking_weighment(self):
        # The arrival snapshots the tare at gate-in, often before it is typed; a
        # frozen 0 must not mask the real tare recorded on the weighment (the
        # 0-kg tare / gross-as-net display on DOCK-20260805-0010).
        arrival = self._arrival("0.000")
        entry = self._docking(arrival=arrival, gross="19700.000", docking_tare="6760.000")
        self.assertEqual(dispatch_tare_weight(entry), Decimal("6760.000"))
        data = SalesDispatchGateOutSerializer(entry).data
        self.assertEqual(Decimal(data["tare_weight"]), Decimal("6760.000"))
        self.assertEqual(Decimal(data["net_weight"]), Decimal("12940.000"))

    def _gate_in(self, arrival):
        ve = VehicleEntry.objects.create(
            entry_no="EVGI-1", company=self.company, vehicle=self.vehicle,
            driver=self.driver, entry_type="EMPTY_VEHICLE", status="COMPLETED",
            created_by=self.user, updated_by=self.user,
        )
        EmptyVehicleGateIn.objects.create(
            company=self.company, entry_no="EVGI-1", vehicle_entry=ve,
            vehicle=self.vehicle, driver=self.driver, reason="DISPATCH",
            arrival=arrival, gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), created_by=self.user, updated_by=self.user,
        )
        return ve

    def test_weighment_tare_edit_propagates_to_arrival(self):
        # The tare is typically typed into the gate-in weighment seconds after the
        # arrival snapshotted 0; the edit must follow onto the arrival, the copy
        # dispatch reads first.
        arrival = self._arrival("0.000")
        ve = self._gate_in(arrival)
        weighment = Weighment.objects.create(
            vehicle_entry=ve, created_by=self.user, updated_by=self.user,
        )
        arrival.refresh_from_db()
        self.assertEqual(arrival.tare_weight, Decimal("0.000"))  # no tare yet: untouched

        weighment.tare_weight = Decimal("6760.000")
        weighment.save()
        arrival.refresh_from_db()
        self.assertEqual(arrival.tare_weight, Decimal("6760.000"))

    def test_docking_weighment_tare_edit_propagates_to_arrival(self):
        arrival = self._arrival("0.000")
        entry = self._docking(arrival=arrival, gross="19700.000", docking_tare="700.000")
        weighment = entry.vehicle_entry.weighment
        weighment.tare_weight = Decimal("6760.000")
        weighment.save()
        arrival.refresh_from_db()
        self.assertEqual(arrival.tare_weight, Decimal("6760.000"))
