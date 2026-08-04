"""Boxes scanned onto a docking are INSIDE_VEHICLE until the truck leaves.

Covers the barcode-side load/unload service (status flip, bin clearing, trail
movements, pallet recalculation, WMS staging), the docking flows that call it
(bill removal), and settlement of INSIDE_VEHICLE boxes at dispatch.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from barcode.models import (
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    Pallet,
    PalletMovement,
    PalletMovementType,
    PalletStatus,
)
from barcode.services.box_ownership import reassign_boxes_to_company
from barcode.services.dispatch_settlement import settle_dispatched_boxes
from barcode.services.vehicle_load import (
    load_boxes_into_vehicle,
    unload_boxes_from_vehicle,
)
from company.models import Company
from wms.models import (
    Inventory as WmsInventory,
    Movement as WmsMovement,
    Pallet as WmsPallet,
)


class InsideVehicleBoxTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create_user(
            email="iv@example.com", password="testpass123",
            full_name="IV User", employee_code="IV001",
        )
        today = timezone.localdate()
        self.pallet = Pallet.objects.create(
            company=self.company, pallet_id="PLT-TEST-001",
            item_code="FG0001", batch_number="B1",
            box_count=2, total_boxes=2, available_boxes=2,
            total_qty=Decimal("40"), mfg_date=today, exp_date=today,
            current_warehouse="FG-WH", current_bin="A-01",
        )
        self.box1 = self._box("BOX-TEST-0001", pallet=self.pallet)
        self.box2 = self._box("BOX-TEST-0002", pallet=self.pallet)

    def _box(self, barcode, pallet=None):
        today = timezone.localdate()
        return Box.objects.create(
            company=self.company, box_barcode=barcode,
            item_code="FG0001", batch_number="B1", qty=Decimal("20"),
            mfg_date=today, exp_date=today, pallet=pallet,
            current_warehouse="FG-WH", current_bin="A-01",
        )

    def _wms_pallet(self):
        wms_pallet = WmsPallet.objects.create(
            company=self.company, record_id="wp-1",
            data={"id": "wp-1", "licensePlate": self.pallet.pallet_id,
                  "currentLocationId": "loc-1", "boxCount": 2, "totalUnits": 40},
        )
        WmsInventory.objects.create(
            company=self.company, record_id="inv-1",
            data={"id": "inv-1", "palletId": "wp-1", "locationId": "loc-1",
                  "boxCount": 2, "quantity": 40},
        )
        return wms_pallet

    # ---- load ------------------------------------------------------------
    def test_load_marks_inside_vehicle_and_clears_location(self):
        loaded = load_boxes_into_vehicle(
            self.company, [self.box1], self.user, reference="Docking DOCK-1"
        )
        self.assertEqual(loaded, 1)
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.INSIDE_VEHICLE)
        self.assertEqual(self.box1.current_bin, "")
        self.assertEqual(self.box1.pre_load_status, BoxStatus.ACTIVE)
        self.assertEqual(self.box1.pre_load_bin, "A-01")
        movement = self.box1.movements.filter(
            movement_type=BoxMovementType.LOAD_VEHICLE
        ).get()
        self.assertEqual(movement.from_bin, "A-01")
        self.assertIn("DOCK-1", movement.notes)
        # One box still active -> pallet is PARTIAL, still at its bin.
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.status, PalletStatus.PARTIAL)
        self.assertEqual(self.pallet.box_count, 1)

    def test_full_pallet_load_stages_pallet_and_frees_wms_location(self):
        self._wms_pallet()
        load_boxes_into_vehicle(
            self.company, [self.box1, self.box2], self.user, reference="Docking DOCK-1"
        )
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.status, PalletStatus.INSIDE_VEHICLE)
        self.assertEqual(self.pallet.current_bin, "")
        self.assertTrue(
            self.pallet.movements.filter(
                movement_type=PalletMovementType.LOAD_VEHICLE
            ).exists()
        )
        # WMS: location freed with an OUTBOUND entry, but the pallet record is
        # kept (un-located) so a reverted load can be re-placed.
        wms_pallet = WmsPallet.objects.get(record_id="wp-1")
        self.assertIsNone(wms_pallet.data.get("currentLocationId"))
        self.assertTrue(wms_pallet.data.get("stagedInVehicle"))
        self.assertEqual(WmsInventory.objects.count(), 0)
        self.assertTrue(
            WmsMovement.objects.filter(company=self.company, data__type="OUTBOUND").exists()
        )

    def test_load_skips_non_loadable_and_is_idempotent(self):
        self.box1.status = BoxStatus.DISPATCHED
        self.box1.save(update_fields=["status"])
        self.assertEqual(
            load_boxes_into_vehicle(self.company, [self.box1], self.user), 0
        )
        load_boxes_into_vehicle(self.company, [self.box2], self.user)
        # Re-loading an INSIDE_VEHICLE box is a no-op.
        self.box2.refresh_from_db()
        self.assertEqual(
            load_boxes_into_vehicle(self.company, [self.box2], self.user), 0
        )
        self.assertEqual(
            self.box2.movements.filter(movement_type=BoxMovementType.LOAD_VEHICLE).count(), 1
        )

    # ---- unload ----------------------------------------------------------
    def test_unload_restores_status_bin_and_pallet(self):
        load_boxes_into_vehicle(
            self.company, [self.box1, self.box2], self.user, reference="Docking DOCK-1"
        )
        unloaded = unload_boxes_from_vehicle(
            self.company, [self.box1, self.box2], self.user, reference="Scan removed"
        )
        self.assertEqual(unloaded, 2)
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.ACTIVE)
        self.assertEqual(self.box1.current_bin, "A-01")
        self.assertEqual(self.box1.pre_load_status, "")
        self.assertTrue(
            self.box1.movements.filter(movement_type=BoxMovementType.UNLOAD_VEHICLE).exists()
        )
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.status, PalletStatus.ACTIVE)
        self.assertEqual(self.pallet.box_count, 2)
        self.assertTrue(
            self.pallet.movements.filter(
                movement_type=PalletMovementType.UNLOAD_VEHICLE
            ).exists()
        )

    def test_unload_preserves_partial_status(self):
        self.box1.status = BoxStatus.PARTIAL
        self.box1.qty = Decimal("5")
        self.box1.save(update_fields=["status", "qty"])
        load_boxes_into_vehicle(self.company, [self.box1], self.user)
        unload_boxes_from_vehicle(self.company, [self.box1], self.user)
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.PARTIAL)

    def test_unload_never_touches_dispatched_boxes(self):
        load_boxes_into_vehicle(self.company, [self.box1], self.user)
        settle_dispatched_boxes(self.company, [self.box1], self.user)
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.DISPATCHED)
        self.assertEqual(
            unload_boxes_from_vehicle(self.company, [self.box1], self.user), 0
        )
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.DISPATCHED)

    def test_chunked_loading_collapses_pallet_trail(self):
        """A pallet loaded in chunks (its boxes surfacing over time) logs ONE
        LOAD per docking and no phantom UNLOADs; only a real unscan logs an
        UNLOAD, after which a re-load logs a fresh LOAD."""
        from barcode.services.pallet_state import recalculate_pallet_state

        # Chunk 1: both current boxes load -> pallet INSIDE_VEHICLE, one LOAD.
        load_boxes_into_vehicle(
            self.company, [self.box1, self.box2], self.user, reference="Docking DOCK-1"
        )
        # A third box of the pallet surfaces to scan (e.g. handed over between
        # chunks) -> pallet drops back to PARTIAL. Not a vehicle event.
        box3 = self._box("BOX-TEST-0003", pallet=self.pallet)
        recalculate_pallet_state(self.company, self.pallet)
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.status, PalletStatus.PARTIAL)
        # Chunk 2 on the SAME docking -> collapses into the existing LOAD.
        load_boxes_into_vehicle(
            self.company, [box3], self.user, reference="Docking DOCK-1"
        )
        vehicle_moves = self.pallet.movements.filter(
            movement_type__in=(
                PalletMovementType.LOAD_VEHICLE, PalletMovementType.UNLOAD_VEHICLE,
            )
        )
        self.assertEqual(
            [m.movement_type for m in vehicle_moves.order_by("performed_at")],
            [PalletMovementType.LOAD_VEHICLE],
        )

        # A chunk on a DIFFERENT docking gets its own LOAD line.
        box4 = self._box("BOX-TEST-0004", pallet=self.pallet)
        recalculate_pallet_state(self.company, self.pallet)
        load_boxes_into_vehicle(
            self.company, [box4], self.user, reference="Docking DOCK-2"
        )
        self.assertEqual(
            list(
                vehicle_moves.order_by("performed_at")
                .values_list("movement_type", "notes")
            ),
            [
                (PalletMovementType.LOAD_VEHICLE, "Docking DOCK-1"),
                (PalletMovementType.LOAD_VEHICLE, "Docking DOCK-2"),
            ],
        )

        # A REAL unscan is an UNLOAD, and re-loading after it is a fresh LOAD.
        unload_boxes_from_vehicle(
            self.company, [box4], self.user, reference="Scan removed — Docking DOCK-2"
        )
        load_boxes_into_vehicle(
            self.company, [box4], self.user, reference="Docking DOCK-2"
        )
        self.assertEqual(
            [m.movement_type for m in vehicle_moves.order_by("performed_at")],
            [
                PalletMovementType.LOAD_VEHICLE,
                PalletMovementType.LOAD_VEHICLE,
                PalletMovementType.UNLOAD_VEHICLE,
                PalletMovementType.LOAD_VEHICLE,
            ],
        )

    # ---- settlement ------------------------------------------------------
    def test_settlement_settles_inside_vehicle_boxes(self):
        self._wms_pallet()
        load_boxes_into_vehicle(self.company, [self.box1, self.box2], self.user)
        result = settle_dispatched_boxes(
            self.company, [self.box1, self.box2], self.user, note="Docking DOCK-1"
        )
        self.assertEqual(result["boxes_dispatched"], 2)
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.status, BoxStatus.DISPATCHED)
        self.assertEqual(self.box1.qty, Decimal("0"))
        self.assertEqual(self.box1.pre_load_status, "")
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.status, PalletStatus.DISPATCHED)
        # Docking settlement now leaves a pallet-level DISPATCH trail row.
        self.assertTrue(
            self.pallet.movements.filter(movement_type=PalletMovementType.DISPATCH).exists()
        )
        # A dispatched pallet's WMS record is fully removed (not just staged).
        self.assertEqual(WmsPallet.objects.count(), 0)

    # ---- ownership trail -------------------------------------------------
    def test_ownership_reassign_writes_trail_movement(self):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        reassign_boxes_to_company(
            [self.box1], mart, user=self.user, reference="BST BST-1"
        )
        self.box1.refresh_from_db()
        self.assertEqual(self.box1.company_id, mart.id)
        movement = BoxMovement.objects.filter(
            box=self.box1, movement_type=BoxMovementType.OWNERSHIP_TRANSFER
        ).get()
        self.assertEqual(movement.company_id, mart.id)
        self.assertIn("JIVO_OIL", movement.notes)
        self.assertIn("JIVO_MART", movement.notes)
        self.assertIn("BST-1", movement.notes)
