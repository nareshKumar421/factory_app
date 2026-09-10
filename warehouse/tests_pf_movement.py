"""Tests for the godown outward-movement register.

The things worth testing more than the CRUD is:

* only a manager of the **source** warehouse may declare movements out of it,
  and the permission alone is not enough (the `UserWarehouse` assignment is the
  second half);
* the destination may be in **another company** — the whole reason the page
  exists is the PF floor pushing stock into Mart's Gupta godown;
* a line's `pieces_per_box` is a snapshot, and a double-typed item is refused
  rather than summed;
* editing replaces the lines and the trail keeps what the document said before;
* reads are NOT warehouse-scoped, on purpose;
* nothing here touches SAP on a write — the register has to be fillable with
  HANA down.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse
from warehouse.models_pf_movement import PFStockMovement, PFStockMovementEvent
from warehouse.services import pf_movement_service

LIST_URL = "/api/v1/warehouse/pf-movements/"
ITEMS_URL = "/api/v1/warehouse/pf-movements/items/"
DESTINATIONS_URL = "/api/v1/warehouse/pf-movements/destinations/"


@override_settings(PF_MOVEMENT_WAREHOUSE="BH-PF")
class PFMovementTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        # The sibling company the Gupta finished godown belongs to. Its presence
        # is the point of half these tests.
        self.mart = Company.objects.create(code="JIVO_MART", name="Jivo Mart")
        self.role = UserRole.objects.create(name="Store")

        # Keeper of the PF floor: both permissions and the assignment.
        self.keeper = self._user("pf@example.com", "PF Keeper", "E-PF")
        self._grant(self.keeper, "can_view_pf_movement", "can_record_pf_movement")
        UserWarehouse.objects.create(
            user=self.keeper, company=self.company, warehouse_code="BH-PF"
        )

        # Holds the record permission but manages no warehouse — the case the
        # `UserWarehouse` half of the rule exists for.
        self.unassigned = self._user("new@example.com", "New Keeper", "E-NEW")
        self._grant(self.unassigned, "can_view_pf_movement", "can_record_pf_movement")

        # Read-only: sees the whole register, declares nothing.
        self.viewer = self._user("plan@example.com", "Planner", "E-PL")
        self._grant(self.viewer, "can_view_pf_movement")

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _grant(self, user, *codenames):
        for codename in codenames:
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def _file(self, user=None, **kwargs):
        payload = {
            "user": user or self.keeper,
            "company": self.company,
            "from_warehouse": "BH-PF",
            "to_warehouse": "BH-BT",
            "to_company": self.company,
            "lines": [
                {
                    "item_code": "FG0001",
                    "item_name": "JIVO OLIVE OIL 1 LTR 20 PCS",
                    "uom": "PCS",
                    "boxes": 12,
                    "pieces_per_box": 20,
                }
            ],
        }
        payload.update(kwargs)
        return pf_movement_service.create_movement(**payload)


class PFMovementServiceTests(PFMovementTestBase):
    def test_filing_a_movement_stores_the_route_lines_and_a_created_event(self):
        movement = self._file(vehicle_no="HR55 1234", remarks="evening load")

        self.assertTrue(movement.entry_no.startswith("PFM-"))
        self.assertEqual(movement.from_warehouse, "BH-PF")
        self.assertEqual(movement.to_warehouse, "BH-BT")
        self.assertEqual(movement.movement_date, timezone.localdate())
        self.assertEqual(movement.created_by, self.keeper)
        self.assertTrue(movement.is_active)

        line = movement.lines.get()
        self.assertEqual(line.item_code, "FG0001")
        self.assertEqual(line.boxes, 12)
        self.assertEqual(line.pieces_per_box, 20)
        # The conversion the dashboard will want, off the snapshot.
        self.assertEqual(line.pieces, 240)

        event = movement.events.get()
        self.assertEqual(event.action, PFStockMovementEvent.Action.CREATED)
        self.assertEqual(event.total_boxes, 12)
        self.assertEqual(event.line_count, 1)
        self.assertEqual(event.changed_by, self.keeper)

    def test_destination_may_be_a_warehouse_of_another_company(self):
        movement = self._file(to_warehouse="GP-FGM", to_company=self.mart)

        self.assertEqual(movement.to_company, self.mart)
        self.assertTrue(movement.is_cross_company)

    def test_the_same_warehouse_of_another_company_is_still_a_movement(self):
        # BH-PF exists in both Oil and Mart. Crossing between their books is a
        # real move whatever the code says.
        movement = self._file(to_warehouse="BH-PF", to_company=self.mart)

        self.assertEqual(movement.to_warehouse, "BH-PF")
        self.assertTrue(movement.is_cross_company)

    def test_sending_to_the_same_warehouse_of_the_same_company_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            self._file(to_warehouse="BH-PF", to_company=self.company)

        self.assertIn("to_warehouse", ctx.exception.detail)

    def test_codes_are_upper_cased_on_the_way_in(self):
        movement = self._file(
            from_warehouse=" bh-pf ",
            to_warehouse="gp-fgm",
            to_company=self.mart,
            lines=[{"item_code": "fg0002", "boxes": 3}],
        )

        self.assertEqual(movement.from_warehouse, "BH-PF")
        self.assertEqual(movement.to_warehouse, "GP-FGM")
        self.assertEqual(movement.lines.get().item_code, "FG0002")

    def test_an_omitted_source_falls_back_to_the_configured_floor(self):
        movement = self._file(from_warehouse="")

        self.assertEqual(movement.from_warehouse, "BH-PF")

    def test_a_keeper_who_manages_no_warehouse_is_refused(self):
        with self.assertRaises(PermissionDenied):
            self._file(user=self.unassigned)

        self.assertFalse(PFStockMovement.objects.exists())

    def test_a_keeper_cannot_declare_out_of_a_floor_he_does_not_manage(self):
        with self.assertRaises(PermissionDenied):
            self._file(from_warehouse="BH-PM")

    def test_a_superuser_is_exempt_from_the_assignment_rule(self):
        root = User.objects.create_superuser(
            email="root@example.com", full_name="Root", employee_code="E-RT",
            password="x",
        )
        UserCompany.objects.create(user=root, company=self.company, role=self.role)

        movement = self._file(user=root, from_warehouse="BH-PM")

        self.assertEqual(movement.from_warehouse, "BH-PM")

    def test_no_lines_is_refused(self):
        with self.assertRaises(ValidationError):
            self._file(lines=[])

    def test_zero_boxes_is_refused(self):
        with self.assertRaises(ValidationError):
            self._file(lines=[{"item_code": "FG0001", "boxes": 0}])

    def test_the_same_item_typed_twice_is_refused_rather_than_summed(self):
        # Summing would hide the keeper's double-entry inside a total nobody
        # can check afterwards.
        with self.assertRaises(ValidationError) as ctx:
            self._file(
                lines=[
                    {"item_code": "FG0001", "boxes": 4},
                    {"item_code": "FG0001", "boxes": 6},
                ]
            )

        self.assertIn("twice", str(ctx.exception.detail))

    def test_a_sap_pack_size_of_zero_is_stored_as_no_pack_size(self):
        # SalFactor2 = 0 is SAP's "not set". Stored as 0 it would make every
        # boxes-to-pieces conversion downstream read zero pieces.
        movement = self._file(
            lines=[{"item_code": "FG0003", "boxes": 5, "pieces_per_box": 0}]
        )

        line = movement.lines.get()
        self.assertIsNone(line.pieces_per_box)
        self.assertIsNone(line.pieces)

    def test_a_date_a_year_out_is_refused_but_tomorrow_is_not(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.assertEqual(self._file(movement_date=tomorrow).movement_date, tomorrow)

        with self.assertRaises(ValidationError):
            self._file(movement_date=timezone.localdate() + timedelta(days=400))

    def test_editing_replaces_the_lines_and_appends_to_the_trail(self):
        movement = self._file()

        pf_movement_service.update_movement(
            user=self.keeper,
            movement=movement,
            to_warehouse="GP-FGM",
            to_company=self.mart,
            lines=[
                {"item_code": "FG0009", "boxes": 30, "pieces_per_box": 12},
                {"item_code": "FG0010", "boxes": 4},
            ],
            note="wrong godown",
        )

        movement.refresh_from_db()
        self.assertEqual(movement.to_warehouse, "GP-FGM")
        self.assertEqual(movement.to_company, self.mart)
        self.assertEqual(movement.updated_by, self.keeper)
        self.assertEqual(
            sorted(movement.lines.values_list("item_code", flat=True)),
            ["FG0009", "FG0010"],
        )

        actions = list(movement.events.order_by("changed_at").values_list(
            "action", "total_boxes"
        ))
        self.assertEqual(
            actions,
            [
                (PFStockMovementEvent.Action.CREATED, 12),
                # The event says what the document holds *after* the edit — the
                # prefetch that arrived with the movement must not be trusted.
                (PFStockMovementEvent.Action.UPDATED, 34),
            ],
        )

    def test_editing_leaves_untouched_fields_alone(self):
        movement = self._file(vehicle_no="HR55 1234", remarks="evening load")

        pf_movement_service.update_movement(
            user=self.keeper, movement=movement, movement_date=timezone.localdate()
        )

        movement.refresh_from_db()
        self.assertEqual(movement.vehicle_no, "HR55 1234")
        self.assertEqual(movement.remarks, "evening load")
        self.assertEqual(movement.lines.count(), 1)

    def test_the_source_warehouse_cannot_be_edited(self):
        # Not an assertion about a refusal — `update_movement` has no parameter
        # for it, and this pins that shut so a later refactor cannot add one
        # silently. Retract and re-file instead.
        movement = self._file()

        with self.assertRaises(TypeError):
            pf_movement_service.update_movement(
                user=self.keeper, movement=movement, from_warehouse="BH-PM"
            )

    def test_cancelling_deactivates_and_keeps_the_document(self):
        movement = self._file()

        pf_movement_service.cancel_movement(
            user=self.keeper, movement=movement, reason="load did not go"
        )

        movement.refresh_from_db()
        self.assertFalse(movement.is_active)
        self.assertEqual(movement.cancelled_by, self.keeper)
        self.assertEqual(movement.cancellation_reason, "load did not go")
        # The lines and the document survive — the declaration having been made
        # is itself a fact the dashboard has to see.
        self.assertEqual(movement.lines.count(), 1)
        self.assertEqual(
            movement.events.latest("changed_at").action,
            PFStockMovementEvent.Action.CANCELLED,
        )

    def test_a_cancelled_movement_cannot_be_edited(self):
        movement = self._file()
        pf_movement_service.cancel_movement(user=self.keeper, movement=movement)

        with self.assertRaises(ValidationError):
            pf_movement_service.update_movement(
                user=self.keeper, movement=movement, vehicle_no="HR55 9999"
            )

    def test_restoring_brings_a_retracted_movement_back(self):
        movement = self._file()
        pf_movement_service.cancel_movement(user=self.keeper, movement=movement)

        pf_movement_service.restore_movement(user=self.keeper, movement=movement)

        movement.refresh_from_db()
        self.assertTrue(movement.is_active)
        self.assertIsNone(movement.cancelled_at)
        self.assertEqual(movement.cancellation_reason, "")
        self.assertEqual(
            movement.events.latest("changed_at").action,
            PFStockMovementEvent.Action.RESTORED,
        )

    def test_cancelled_movements_are_out_of_the_list_unless_asked_for(self):
        kept = self._file()
        dropped = self._file(to_warehouse="GP-FGM", to_company=self.mart)
        pf_movement_service.cancel_movement(user=self.keeper, movement=dropped)

        default = pf_movement_service.list_movements(company_code=self.company.code)
        self.assertEqual([m.id for m in default], [kept.id])

        with_cancelled = pf_movement_service.list_movements(
            company_code=self.company.code, include_cancelled=True
        )
        self.assertEqual(with_cancelled.count(), 2)

    def test_the_search_matches_the_document_and_its_contents(self):
        movement = self._file(vehicle_no="HR55 1234")
        self._file(
            to_warehouse="GP-FGM",
            to_company=self.mart,
            lines=[{"item_code": "FG9999", "boxes": 1}],
        )

        by_item = pf_movement_service.list_movements(
            company_code=self.company.code, search="FG0001"
        )
        self.assertEqual([m.id for m in by_item], [movement.id])

        by_vehicle = pf_movement_service.list_movements(
            company_code=self.company.code, search="hr55"
        )
        self.assertEqual([m.id for m in by_vehicle], [movement.id])

    def test_the_summary_totals_boxes_over_the_same_filter(self):
        self._file(lines=[{"item_code": "FG0001", "boxes": 12}])
        self._file(
            to_warehouse="GP-FGM",
            to_company=self.mart,
            lines=[
                {"item_code": "FG0002", "boxes": 8},
                {"item_code": "FG0003", "boxes": 5},
            ],
        )

        summary = pf_movement_service.summarise(
            pf_movement_service.list_movements(company_code=self.company.code)
        )

        self.assertEqual(summary, {"movements": 2, "total_boxes": 25})

    def test_the_summary_survives_a_search(self):
        # Searching makes the list DISTINCT, and a summary subquery that kept
        # the list's ordering is rejected by Postgres (`SELECT DISTINCT id`
        # ordered by a column not in the select list). sqlite does not
        # reproduce it, so this test pins the shape rather than the error.
        self._file(vehicle_no="HR55 1234", lines=[{"item_code": "FG0001", "boxes": 9}])
        self._file(
            to_warehouse="GP-FGM",
            to_company=self.mart,
            lines=[{"item_code": "FG0002", "boxes": 4}],
        )

        summary = pf_movement_service.summarise(
            pf_movement_service.list_movements(
                company_code=self.company.code, search="FG0001"
            )
        )

        self.assertEqual(summary, {"movements": 1, "total_boxes": 9})

    def test_filing_never_touches_sap(self):
        # The floor must stay able to file with HANA down. A reader constructed
        # on a write path would blow up here.
        with patch("warehouse.services.pf_movement_service.WMSHanaReader") as reader:
            self._file()

        reader.assert_not_called()


class PFMovementAPITests(PFMovementTestBase):
    def test_a_keeper_can_file_a_cross_company_movement_over_the_api(self):
        response = self._client(self.keeper).post(
            LIST_URL,
            {
                "from_warehouse": "BH-PF",
                "to_warehouse": "GP-FGM",
                "to_company": self.mart.id,
                "to_warehouse_name": "GUPTA FINISHED GOODS MART",
                "vehicle_no": "HR55 1234",
                "lines": [
                    {
                        "item_code": "FG0001",
                        "item_name": "JIVO OLIVE OIL 1 LTR 20 PCS",
                        "uom": "PCS",
                        "boxes": 12,
                        "pieces_per_box": 20,
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["to_warehouse"], "GP-FGM")
        self.assertEqual(response.data["to_company_code"], "JIVO_MART")
        self.assertTrue(response.data["is_cross_company"])
        self.assertEqual(response.data["total_boxes"], 12)
        self.assertEqual(response.data["lines"][0]["pieces"], 240)

    def test_a_viewer_may_read_the_register_but_not_file(self):
        self._file()

        listed = self._client(self.viewer).get(LIST_URL)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data["movements"]), 1)
        self.assertEqual(listed.data["summary"]["total_boxes"], 12)

        refused = self._client(self.viewer).post(
            LIST_URL,
            {
                "to_warehouse": "BH-BT",
                "to_company": self.company.id,
                "lines": [{"item_code": "FG0001", "boxes": 1}],
            },
            format="json",
        )
        self.assertEqual(refused.status_code, 403)

    def test_reads_are_not_warehouse_scoped(self):
        # A register whose totals depend on who is looking cannot be read
        # against anything. The viewer manages nothing and still sees the row.
        self._file()

        response = self._client(self.viewer).get(LIST_URL)

        self.assertEqual(len(response.data["movements"]), 1)
        self.assertEqual(response.data["managed_warehouse_codes"], [])

    def test_the_list_reports_the_default_floor_and_what_the_caller_manages(self):
        response = self._client(self.keeper).get(LIST_URL)

        self.assertEqual(response.data["default_from_warehouse"], "BH-PF")
        self.assertEqual(response.data["managed_warehouse_codes"], ["BH-PF"])
        self.assertFalse(response.data["unrestricted"])

    def test_the_detail_endpoint_carries_the_trail(self):
        movement = self._file()

        response = self._client(self.keeper).get(f"{LIST_URL}{movement.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["movement"]["entry_no"], movement.entry_no)
        self.assertEqual(len(response.data["history"]), 1)
        self.assertEqual(response.data["history"][0]["action"], "CREATED")

    def test_patching_replaces_the_lines_in_the_response(self):
        movement = self._file()

        response = self._client(self.keeper).patch(
            f"{LIST_URL}{movement.id}/",
            {"lines": [{"item_code": "FG0077", "boxes": 7}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["lines"]), 1)
        self.assertEqual(response.data["lines"][0]["item_code"], "FG0077")
        self.assertEqual(response.data["total_boxes"], 7)

    def test_deleting_cancels_with_the_reason_given(self):
        movement = self._file()

        response = self._client(self.keeper).delete(
            f"{LIST_URL}{movement.id}/", {"reason": "truck never came"}, format="json"
        )

        self.assertEqual(response.status_code, 204)
        movement.refresh_from_db()
        self.assertFalse(movement.is_active)
        self.assertEqual(movement.cancellation_reason, "truck never came")

    def test_restoring_over_the_api(self):
        movement = self._file()
        pf_movement_service.cancel_movement(user=self.keeper, movement=movement)

        response = self._client(self.keeper).post(
            f"{LIST_URL}{movement.id}/restore/", {}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_active"])

    def test_a_movement_of_another_company_is_not_reachable_by_id(self):
        movement = self._file()
        PFStockMovement.objects.filter(pk=movement.pk).update(company=self.mart)

        response = self._client(self.keeper).get(f"{LIST_URL}{movement.id}/")

        self.assertEqual(response.status_code, 404)

    def test_the_item_picker_asks_sap_for_finished_goods_only(self):
        with patch(
            "warehouse.services.pf_movement_service.WMSHanaReader"
        ) as reader_class:
            reader_class.return_value.search_items_in_group.return_value = [
                {
                    "item_code": "FG0001",
                    "item_name": "JIVO OLIVE OIL 1 LTR 20 PCS",
                    "uom": "PCS",
                    "pieces_per_box": 20,
                    "sap_on_hand": 480.0,
                }
            ]
            response = self._client(self.keeper).get(ITEMS_URL, {"search": "olive"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items"][0]["pieces_per_box"], 20)
        kwargs = reader_class.return_value.search_items_in_group.call_args.kwargs
        self.assertEqual(kwargs["item_group_code"], 102)
        # The on-hand shown is the source floor's, not some other store's.
        self.assertEqual(kwargs["warehouse_code"], "BH-PF")

    def test_the_destination_picker_spans_companies(self):
        with patch(
            "warehouse.services.pf_movement_service.WMSHanaReader"
        ) as reader_class:
            reader_class.return_value.get_warehouses.return_value = [
                {"code": "BH-BT", "name": "Bhakharpur New Basement"}
            ]
            response = self._client(self.keeper).get(DESTINATIONS_URL)

        self.assertEqual(response.status_code, 200)
        codes = sorted(c["company_code"] for c in response.data["companies"])
        self.assertEqual(codes, ["JIVO_MART", "JIVO_OIL"])

    def test_one_company_failing_does_not_empty_the_destination_picker(self):
        class _Reader:
            def __init__(self, company_code):
                self.company_code = company_code

            def get_warehouses(self):
                if self.company_code == "JIVO_MART":
                    raise RuntimeError("HANA unreachable")
                return [{"code": "BH-BT", "name": "Bhakharpur New Basement"}]

        with patch(
            "warehouse.services.pf_movement_service.WMSHanaReader", _Reader
        ):
            response = self._client(self.keeper).get(DESTINATIONS_URL)

        self.assertEqual(response.status_code, 200)
        by_code = {c["company_code"]: c for c in response.data["companies"]}
        self.assertEqual(len(by_code["JIVO_OIL"]["warehouses"]), 1)
        self.assertEqual(by_code["JIVO_MART"]["warehouses"], [])
        self.assertIn("HANA unreachable", by_code["JIVO_MART"]["error"])
