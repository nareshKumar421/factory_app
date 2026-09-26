"""
BOM Changes through the real permission stack.

    DEBUG=False python manage.py test bom_changes --settings=config.sqlite_test_settings

Every request goes through IsAuthenticated + HasCompanyContext + a BOM right,
so these also pin a missing Company-Code header and each right. SAP is mocked
where each module looks ``SAPClient`` up: ``bom_changes.services`` (the
existence check, the snapshot and the writes) and ``bom_changes.views`` (the
BOM viewer). Nothing here reaches SAP.

SQLite takes no row locks, so the ``select_for_update`` in ``services`` is
exercised only for its query shape here; PostgreSQL proves the lock.
"""

from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPValidationError

from . import permissions as guards
from . import services, workflow
from .management.commands.setup_bom_changes_groups import BOM_CHANGES_GROUPS
from .models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest

BASE = "/api/v1/bom-changes/"
REQUESTS = f"{BASE}requests/"
ALL_PERMISSIONS = [
    "can_view_bom_changes",
    "can_request_bom_changes",
    "can_approve_bom_level_1",
    "can_approve_bom_level_2",
    "can_push_bom_to_sap",
    "can_push_bom_directly",
]

NEW_BOM = {
    "kind": "CREATE",
    "item_code": "fg0000121",
    "item_name": "CANOLA OIL 1 LTR 20 PCS",
    "quantity": "20",
    "bom_type": "Production",
    "warehouse": "BH-PF",
    "distribution_rule": "OIL",
    "project": "",
    "lines": [
        {"item_code": "rm0001", "item_name": "Canola oil", "quantity": "20", "unit_cost": "150.5",
         "warehouse": "", "issue_method": "Backflush", "comment": "loose oil"},
        {"item_code": "PM0002", "quantity": "1", "warehouse": "BH-PC"},
        {"item_type": "resource", "item_code": "JWPL09240001", "item_name": "Filling cost", "quantity": "20"},
    ],
}

SAP_TREE = {
    "tree_code": "FG0000121",
    "description": "CANOLA OIL 1 LTR 20 PCS",
    "tree_type": "P",
    "bom_type": "Production",
    "sap_tree_type": "iProductionTree",
    "quantity": 20.0,
    "warehouse": "BH-PF",
    "distribution_rule": "OIL",
    "project": "P1",
    "price_list": -1,
    "updated_at": "2026-09-01",
    "lines": [
        {"child_num": 0, "visual_order": 0, "item_type": "item", "item_code": "RM0001",
         "item_name": "Canola oil", "quantity": 20.0, "warehouse": "BH-PF", "issue_method": "Backflush",
         "unit_cost": 150.5, "currency": "INR", "comment": "", "uom": "LTR"},
    ],
    "item_count": 1,
    "resource_count": 0,
}


def fake_sap(exists=False, tree=None):
    sap = MagicMock()
    sap.product_tree_exists.return_value = exists
    sap.get_product_tree.return_value = tree
    sap.create_product_tree.return_value = {"tree_code": "FG0000121"}
    sap.replace_product_tree.return_value = {"tree_code": "FG0000121"}
    return sap


class BomChangesTestCase(APITestCase):
    """One company and a user per role; each test grants what it needs."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Staff")
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self._n = 0
        self.requester = self.make_user("Requester", "can_request_bom_changes")
        self.level_1 = self.make_user("Manager", "can_approve_bom_level_1")
        self.level_2 = self.make_user("Senior Manager", "can_approve_bom_level_2")
        self.pusher = self.make_user("SAP Adder", "can_push_bom_to_sap")
        self.pusher_2 = self.make_user("SAP Adder Two", "can_push_bom_to_sap")
        self.viewer = self.make_user("Viewer", "can_view_bom_changes")

    def make_user(self, name, *codenames, company=None):
        self._n += 1
        user = get_user_model().objects.create_user(
            email=f"bom{self._n}@example.com",
            password="testpass",
            full_name=name,
            employee_code=f"BOM{self._n:03d}",
        )
        UserCompany.objects.create(
            user=user, company=company or self.company, role=self.role, is_default=True
        )
        if codenames:
            user.user_permissions.add(
                *Permission.objects.filter(content_type__app_label="bom_changes", codename__in=codenames)
            )
        # has_perm() caches per instance: hand back a fresh one.
        return get_user_model().objects.get(pk=user.pk)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def post(self, user, path, body=None):
        return self.client_for(user).post(path, body or {}, format="json", **self.headers)

    def get(self, user, path):
        return self.client_for(user).get(path, **self.headers)

    def raise_request(self, sap_class, body=None, user=None):
        sap_class.return_value = fake_sap()
        response = self.post(user or self.requester, REQUESTS, body or NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["id"]

    def approve(self, user, pk, remarks=""):
        return self.post(user, f"{REQUESTS}{pk}/approve/", {"remarks": remarks})


# ---------------------------------------------------------------------------
# The ladder itself
# ---------------------------------------------------------------------------


class WorkflowRuleTests(SimpleTestCase):
    def test_three_levels_is_level_1_level_2_then_the_push(self):
        self.assertEqual(workflow.right_for("PENDING", 3), guards.LEVEL_1_PERMISSION)
        self.assertEqual(workflow.right_for("L1_APPROVED", 3), guards.LEVEL_2_PERMISSION)
        self.assertEqual(workflow.right_for("L2_APPROVED", 3), guards.PUSH_PERMISSION)
        self.assertEqual(
            [workflow.next_status(s, 3) for s in ("PENDING", "L1_APPROVED", "L2_APPROVED")],
            ["L1_APPROVED", "L2_APPROVED", "SAP_PUSHED"],
        )
        self.assertEqual([workflow.is_final(s, 3) for s in ("PENDING", "L1_APPROVED", "L2_APPROVED")],
                         [False, False, True])

    def test_two_levels_pushes_after_level_1(self):
        self.assertEqual(workflow.right_for("PENDING", 2), guards.LEVEL_1_PERMISSION)
        self.assertEqual(workflow.right_for("L1_APPROVED", 2), guards.PUSH_PERMISSION)
        self.assertTrue(workflow.is_final("L1_APPROVED", 2))
        self.assertEqual(workflow.next_status("L1_APPROVED", 2), "SAP_PUSHED")

    def test_four_levels_needs_two_pushes(self):
        self.assertEqual(workflow.right_for("L2_APPROVED", 4), guards.PUSH_PERMISSION)
        self.assertFalse(workflow.is_final("L2_APPROVED", 4))
        self.assertEqual(workflow.next_status("L2_APPROVED", 4), "L3_APPROVED")
        self.assertEqual(workflow.right_for("L3_APPROVED", 4), guards.PUSH_PERMISSION)
        self.assertTrue(workflow.is_final("L3_APPROVED", 4))

    def test_a_lowered_setting_does_not_strand_a_request(self):
        self.assertTrue(workflow.is_final("L2_APPROVED", 2))
        self.assertEqual(workflow.right_for("L3_APPROVED", 3), guards.PUSH_PERMISSION)
        self.assertEqual(workflow.level_of("L3_APPROVED", 3), 3)

    def test_closed_requests_have_nothing_to_decide(self):
        for closed in ("SAP_PUSHED", "REJECTED", "CANCELLED"):
            self.assertIsNone(workflow.right_for(closed, 3))
            self.assertIsNone(workflow.next_status(closed, 3))

    @override_settings(BOM_CHANGE_APPROVAL_LEVELS=5)
    def test_an_unknown_number_of_levels_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            workflow.approval_levels()

    def test_steps_name_what_writes_sap(self):
        self.assertEqual([s["writes_sap"] for s in workflow.steps(4)], [False, False, False, True])
        self.assertEqual([s["level"] for s in workflow.steps(2)], [1, 2])


# ---------------------------------------------------------------------------
# The state machine through the API
# ---------------------------------------------------------------------------


@patch("bom_changes.services.SAPClient")
class StateMachineTests(BomChangesTestCase):
    @override_settings(BOM_CHANGE_APPROVAL_LEVELS=3)
    def test_three_levels_end_in_one_sap_write(self, sap_class):
        pk = self.raise_request(sap_class)
        sap = fake_sap()
        sap_class.return_value = sap

        self.assertEqual(self.approve(self.level_1, pk).data["status"], "L1_APPROVED")
        self.assertEqual(self.approve(self.level_2, pk).data["status"], "L2_APPROVED")
        sap.create_product_tree.assert_not_called()
        response = self.approve(self.pusher, pk, "looks right")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "SAP_PUSHED")
        self.assertEqual(response.data["sap_result"], {"tree_code": "FG0000121", "operation": "CREATED"})
        sap.create_product_tree.assert_called_once()
        sap_class.assert_called_with(company_code="JIVO_OIL")

        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual(row.sap_pushed_by, self.pusher)
        self.assertIsNotNone(row.sap_pushed_at)
        self.assertEqual(
            list(row.approvals.values_list("level", "from_status", "action")),
            [(1, "PENDING", "APPROVE"), (2, "L1_APPROVED", "APPROVE"), (3, "L2_APPROVED", "APPROVE")],
        )
        self.assertEqual([s["state"] for s in response.data["steps"]], ["done", "done", "done"])

    @override_settings(BOM_CHANGE_APPROVAL_LEVELS=2)
    def test_two_levels_push_after_level_1(self, sap_class):
        pk = self.raise_request(sap_class)
        self.assertEqual(self.approve(self.level_1, pk).data["status"], "L1_APPROVED")
        # Level 2 has no step of its own with two levels.
        self.assertEqual(self.approve(self.level_2, pk).status_code, status.HTTP_403_FORBIDDEN)
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.data["status"], "SAP_PUSHED")
        self.assertEqual(response.data["approvals"][-1]["level"], 2)

    @override_settings(BOM_CHANGE_APPROVAL_LEVELS=4)
    def test_four_levels_need_two_different_pushers(self, sap_class):
        pk = self.raise_request(sap_class)
        sap = fake_sap()
        sap_class.return_value = sap
        self.approve(self.level_1, pk)
        self.approve(self.level_2, pk)
        first = self.approve(self.pusher, pk)
        self.assertEqual(first.data["status"], "L3_APPROVED")
        sap.create_product_tree.assert_not_called()

        again = self.approve(self.pusher, pk)
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already approved", again.data["detail"])

        final = self.approve(self.pusher_2, pk)
        self.assertEqual(final.data["status"], "SAP_PUSHED")
        sap.create_product_tree.assert_called_once()
        self.assertEqual([a["level"] for a in final.data["approvals"]], [1, 2, 3, 4])

    def test_one_person_never_signs_two_levels(self, sap_class):
        both = self.make_user("Both levels", "can_approve_bom_level_1", "can_approve_bom_level_2")
        pk = self.raise_request(sap_class)
        self.assertEqual(self.approve(both, pk).data["status"], "L1_APPROVED")
        response = self.approve(both, pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # ... and cannot reject that level either: one rule for both actions.
        reject = self.post(both, f"{REQUESTS}{pk}/reject/", {"remarks": "no"})
        self.assertEqual(reject.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BOMChangeRequest.objects.get(pk=pk).status, "L1_APPROVED")

    def test_the_wrong_level_is_refused(self, sap_class):
        pk = self.raise_request(sap_class)
        response = self.approve(self.level_2, pk)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Level 1 approval", response.data["detail"])
        self.assertEqual(self.approve(self.pusher, pk).status_code, status.HTTP_403_FORBIDDEN)

    def test_whoever_may_approve_may_reject_and_it_closes_the_request(self, sap_class):
        pk = self.raise_request(sap_class)
        response = self.post(self.level_1, f"{REQUESTS}{pk}/reject/", {"remarks": "wrong pack"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "REJECTED")
        self.assertEqual(response.data["approvals"][0]["remarks"], "wrong pack")
        self.assertEqual(response.data["steps"][0]["state"], "rejected")
        # Closed: nothing left to approve.
        self.assertEqual(self.approve(self.level_1, pk).status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(BOM_CHANGE_APPROVAL_LEVELS=3)
    def test_rejecting_the_final_step_needs_the_push_right(self, sap_class):
        pk = self.raise_request(sap_class)
        self.approve(self.level_1, pk)
        self.approve(self.level_2, pk)
        other_l2 = self.make_user("Another senior", "can_approve_bom_level_2")
        reject = self.post(other_l2, f"{REQUESTS}{pk}/reject/")
        self.assertEqual(reject.status_code, status.HTTP_403_FORBIDDEN)
        reject = self.post(self.pusher, f"{REQUESTS}{pk}/reject/", {"remarks": "not in SAP"})
        self.assertEqual(reject.data["status"], "REJECTED")
        sap_class.return_value.create_product_tree.assert_not_called()

    def test_the_submitter_cancels_a_pending_request(self, sap_class):
        pk = self.raise_request(sap_class)
        response = self.post(self.requester, f"{REQUESTS}{pk}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "CANCELLED")
        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual(row.cancelled_by, self.requester)

    def test_only_the_submitter_or_a_pusher_cancels(self, sap_class):
        pk = self.raise_request(sap_class)
        someone = self.make_user("Other requester", "can_request_bom_changes")
        self.assertEqual(self.post(someone, f"{REQUESTS}{pk}/cancel/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post(self.level_1, f"{REQUESTS}{pk}/cancel/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post(self.pusher, f"{REQUESTS}{pk}/cancel/").data["status"], "CANCELLED")

    def test_an_approved_request_cannot_be_cancelled(self, sap_class):
        pk = self.raise_request(sap_class)
        self.approve(self.level_1, pk)
        response = self.post(self.requester, f"{REQUESTS}{pk}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BOMChangeRequest.objects.get(pk=pk).status, "L1_APPROVED")

    def test_rows_say_what_the_caller_may_do(self, sap_class):
        pk = self.raise_request(sap_class)
        mine = self.get(self.requester, f"{REQUESTS}{pk}/").data
        self.assertEqual(
            {k: mine[k] for k in ("can_approve", "can_reject", "can_cancel", "can_push")},
            {"can_approve": False, "can_reject": False, "can_cancel": True, "can_push": False},
        )
        manager = self.get(self.level_1, f"{REQUESTS}{pk}/").data
        self.assertTrue(manager["can_approve"] and manager["can_reject"])
        self.assertFalse(manager["can_push"])
        self.approve(self.level_1, pk)
        self.approve(self.level_2, pk)
        adder = self.get(self.pusher, f"{REQUESTS}{pk}/").data
        self.assertTrue(adder["can_push"])
        self.assertEqual(adder["awaiting"], "Final approval (writes SAP)")

    def test_my_turn_lists_only_what_i_can_sign(self, sap_class):
        first = self.raise_request(sap_class)
        second = self.raise_request(sap_class, dict(NEW_BOM, item_code="FG0000122"))
        self.approve(self.level_1, first)
        both = self.make_user("Both", "can_approve_bom_level_1", "can_approve_bom_level_2")
        data = self.get(both, f"{REQUESTS}?actionable=true").data
        self.assertEqual({row["id"] for row in data["results"]}, {first, second})
        self.approve(both, second)
        data = self.get(both, f"{REQUESTS}?actionable=true").data
        # Second is now at level 2, but they signed level 1 of it.
        self.assertEqual([row["id"] for row in data["results"]], [first])
        self.assertEqual(data["counts"]["ACTIONABLE"], 1)
        self.assertEqual(data["counts"]["L1_APPROVED"], 2)

    def test_list_filters(self, sap_class):
        first = self.raise_request(sap_class)
        self.raise_request(sap_class, dict(NEW_BOM, item_code="FG0000999"), user=self.make_user(
            "Another", "can_request_bom_changes"))
        self.approve(self.level_1, first)
        self.assertEqual(
            [r["id"] for r in self.get(self.viewer, f"{REQUESTS}?status=L1_APPROVED").data["results"]], [first]
        )
        self.assertEqual(len(self.get(self.requester, f"{REQUESTS}?mine=true").data["results"]), 1)
        self.assertEqual(len(self.get(self.viewer, f"{REQUESTS}?search=0999").data["results"]), 1)
        self.assertEqual(len(self.get(self.viewer, f"{REQUESTS}?kind=UPDATE").data["results"]), 0)
        self.assertEqual(self.get(self.viewer, f"{REQUESTS}?status=NOPE").status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Raising a request
# ---------------------------------------------------------------------------


@patch("bom_changes.services.SAPClient")
class RaiseRequestTests(BomChangesTestCase):
    def test_a_new_bom_request_is_saved_with_its_lines(self, sap_class):
        pk = self.raise_request(sap_class)
        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual((row.item_code, row.status, row.created_by), ("FG0000121", "PENDING", self.requester))
        self.assertEqual(
            list(row.lines.values_list("visual_order", "item_code", "item_type")),
            [(0, "RM0001", "item"), (1, "PM0002", "item"), (2, "JWPL09240001", "resource")],
        )
        sap_class.return_value.product_tree_exists.assert_called_once_with("FG0000121")

    def test_a_new_bom_for_an_item_that_has_one_is_409(self, sap_class):
        sap_class.return_value = fake_sap(exists=True)
        response = self.post(self.requester, REQUESTS, NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(BOMChangeRequest.objects.exists())

    def test_a_change_needs_a_bom_in_sap(self, sap_class):
        sap_class.return_value = fake_sap(tree=None)
        body = {"kind": "UPDATE", "item_code": "FG0000121", "lines": [{"item_code": "RM1", "quantity": "1"}]}
        response = self.post(self.requester, REQUESTS, body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("item_code", response.data)

    def test_a_change_takes_what_it_leaves_out_from_sap(self, sap_class):
        sap_class.return_value = fake_sap(tree=SAP_TREE)
        body = {"kind": "UPDATE", "item_code": "FG0000121", "quantity": "24",
                "lines": [{"item_code": "RM1", "quantity": "2"}]}
        response = self.post(self.requester, REQUESTS, body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        row = BOMChangeRequest.objects.get(pk=response.data["id"])
        self.assertEqual(row.quantity, Decimal("24"))  # given
        self.assertEqual((row.item_name, row.warehouse, row.distribution_rule, row.project, row.bom_type),
                         ("CANOLA OIL 1 LTR 20 PCS", "BH-PF", "OIL", "P1", "Production"))
        self.assertEqual(row.original_data["tree_code"], "FG0000121")

    def test_what_sap_would_refuse_is_refused_first(self, sap_class):
        sap_class.return_value = fake_sap()
        cases = {
            "no lines": dict(NEW_BOM, lines=[]),
            "zero quantity": dict(NEW_BOM, lines=[{"item_code": "RM1", "quantity": "0"}]),
            "its own parent": dict(NEW_BOM, lines=[{"item_code": "FG0000121", "quantity": "1"}]),
            "no name for a new BOM": dict(NEW_BOM, item_name=""),
            "comment past 100": dict(NEW_BOM, lines=[{"item_code": "RM1", "quantity": "1", "comment": "x" * 101}]),
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.assertEqual(self.post(self.requester, REQUESTS, body).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BOMChangeRequest.objects.exists())

    def test_sap_down_at_submission_is_503_and_saves_nothing(self, sap_class):
        sap_class.return_value.product_tree_exists.side_effect = SAPConnectionError("Unable to connect to SAP HANA.")
        response = self.post(self.requester, REQUESTS, NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(BOMChangeRequest.objects.exists())


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------


class PayloadTests(TestCase):
    """The ProductTrees bodies, exactly as the portal's pushBomToSap built them."""

    def setUp(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.request = BOMChangeRequest.objects.create(
            company=company, kind="CREATE", item_code="FG0000121", item_name="N" * 120,
            quantity=Decimal("20"), bom_type="Sales", warehouse="BH-PF", distribution_rule="OIL",
            project="",
        )
        self.lines = [
            BOMChangeLine(request=self.request, visual_order=5, item_code="rm0001 ", quantity=Decimal("20"),
                          issue_method="Backflush", warehouse="", unit_cost=Decimal("150.5"), comment=" loose "),
            BOMChangeLine(request=self.request, visual_order=7, item_code="JWPL1", item_type="resource",
                          quantity=Decimal("2.5"), warehouse="BH-PC", unit_cost=Decimal("0")),
        ]

    def test_create(self):
        payload = services.build_create_payload(self.request, self.lines)
        self.assertEqual(
            payload,
            {
                "TreeCode": "FG0000121",
                "TreeType": "iSalesTree",
                "Quantity": 20.0,
                "ProductDescription": "N" * 100,
                "PriceList": -1,
                "Warehouse": "BH-PF",
                "DistributionRule": "OIL",
                "ProductTreeLines": [
                    {"ItemCode": "RM0001", "Quantity": 20.0, "IssueMethod": "im_Backflush", "ItemType": "pit_Item",
                     "VisualOrder": 0, "PriceList": -1, "Warehouse": "BH-PF", "Price": 150.5, "Currency": "INR",
                     "Comment": "loose"},
                    {"ItemCode": "JWPL1", "Quantity": 2.5, "IssueMethod": "im_Manual", "ItemType": "pit_Resource",
                     "VisualOrder": 1, "PriceList": -1, "Warehouse": "BH-PC"},
                ],
            },
        )

    def test_update_keeps_the_portals_differences(self):
        self.request.kind = "UPDATE"
        self.request.warehouse = ""
        payload = services.build_update_payload(self.request, self.lines)
        self.assertEqual(payload["Warehouse"], "")  # always sent, even empty
        self.assertNotIn("TreeCode", payload)  # the key is in the URL
        self.assertEqual(payload["ProductDescription"], "N" * 100)
        self.assertEqual(payload["DistributionRule"], "OIL")
        self.assertEqual(
            payload["ProductTreeLines"],
            [
                {"ItemCode": "RM0001", "Quantity": 20.0, "IssueMethod": "im_Backflush", "ItemType": "pit_Item",
                 "VisualOrder": 5, "PriceList": -1},
                {"ItemCode": "JWPL1", "Quantity": 2.5, "IssueMethod": "im_Manual", "ItemType": "pit_Resource",
                 "VisualOrder": 7, "PriceList": -1, "Warehouse": "BH-PC"},
            ],
        )

    def test_update_sends_a_description_only_when_there_is_one(self):
        self.request.item_name = ""
        self.assertNotIn("ProductDescription", services.build_update_payload(self.request, self.lines))

    def test_an_unknown_bom_type_goes_as_a_production_tree(self):
        self.request.bom_type = "Mystery"
        self.assertEqual(services.build_create_payload(self.request, self.lines)["TreeType"], "iProductionTree")


@override_settings(BOM_CHANGE_APPROVAL_LEVELS=2)
@patch("bom_changes.services.SAPClient")
class PushTests(BomChangesTestCase):
    """The final approval: guards, then SAP, then the row."""

    def at_final_step(self, sap_class, body=None):
        pk = self.raise_request(sap_class, body)
        self.approve(self.level_1, pk)
        return pk

    def test_a_new_bom_sap_already_has_is_409_and_nothing_is_posted(self, sap_class):
        pk = self.at_final_step(sap_class)
        sap_class.return_value = fake_sap(exists=True)
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        sap_class.return_value.create_product_tree.assert_not_called()
        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual(row.status, "L1_APPROVED")
        self.assertIn("already has a BOM", row.push_error)
        self.assertEqual(row.approvals.count(), 1)  # the final approval rolled back

    def test_when_sap_cannot_say_whether_it_has_the_bom_nothing_is_posted(self, sap_class):
        pk = self.at_final_step(sap_class)
        sap = fake_sap()
        sap.product_tree_exists.side_effect = SAPConnectionError("Unable to connect to SAP HANA.")
        sap_class.return_value = sap
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        sap.create_product_tree.assert_not_called()
        self.assertEqual(BOMChangeRequest.objects.get(pk=pk).status, "L1_APPROVED")

    def test_sap_refusing_leaves_the_status_and_says_why(self, sap_class):
        pk = self.at_final_step(sap_class)
        sap = fake_sap()
        sap.create_product_tree.side_effect = SAPValidationError("(-5002) Item 'RM0001' is inactive")
        sap_class.return_value = sap
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("RM0001' is inactive", response.data["detail"])
        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual(row.status, "L1_APPROVED")
        self.assertIsNone(row.sap_pushed_at)
        self.assertIn("inactive", row.push_error)
        self.assertIsNotNone(row.push_failed_at)
        # Another try that works clears the failure.
        sap.create_product_tree.side_effect = None
        self.assertEqual(self.approve(self.pusher, pk).data["status"], "SAP_PUSHED")
        self.assertEqual(BOMChangeRequest.objects.get(pk=pk).push_error, "")

    def test_a_timed_out_write_says_to_check_sap(self, sap_class):
        pk = self.at_final_step(sap_class)
        sap = fake_sap()
        sap.create_product_tree.side_effect = SAPConnectionError(
            "SAP did not answer in time. It may still have saved the change — check SAP before trying again."
        )
        sap_class.return_value = sap
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("check SAP", response.data["detail"])

    def test_a_change_replaces_the_tree_and_keeps_what_it_replaced(self, sap_class):
        sap_class.return_value = fake_sap(tree=SAP_TREE)
        body = {"kind": "UPDATE", "item_code": "FG0000121",
                "lines": [{"item_code": "RM0001", "quantity": "21", "unit_cost": "10", "comment": "more"}]}
        response = self.post(self.requester, REQUESTS, body)
        pk = response.data["id"]
        self.approve(self.level_1, pk)
        newer = dict(SAP_TREE, quantity=19.0)
        sap = fake_sap(tree=newer)
        sap_class.return_value = sap
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.data["status"], "SAP_PUSHED")
        sap.create_product_tree.assert_not_called()
        code, payload = sap.replace_product_tree.call_args[0]
        self.assertEqual(code, "FG0000121")
        self.assertEqual(payload["ProductTreeLines"][0]["Quantity"], 21.0)
        self.assertNotIn("Price", payload["ProductTreeLines"][0])
        self.assertNotIn("Comment", payload["ProductTreeLines"][0])
        row = BOMChangeRequest.objects.get(pk=pk)
        self.assertEqual(row.original_data["quantity"], 19.0)  # read just before the PUT
        self.assertEqual(row.sap_result, {"tree_code": "FG0000121", "operation": "UPDATED"})

    def test_a_change_to_a_bom_sap_no_longer_has_is_refused(self, sap_class):
        sap_class.return_value = fake_sap(tree=SAP_TREE)
        body = {"kind": "UPDATE", "item_code": "FG0000121", "lines": [{"item_code": "RM0001", "quantity": "1"}]}
        pk = self.post(self.requester, REQUESTS, body).data["id"]
        self.approve(self.level_1, pk)
        sap_class.return_value = fake_sap(tree=None)
        response = self.approve(self.pusher, pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sap_class.return_value.replace_product_tree.assert_not_called()


@patch("bom_changes.services.SAPClient")
class DirectPushTests(BomChangesTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("Admin", "can_push_bom_directly")

    def test_a_direct_push_writes_sap_and_records_a_level_0_approval(self, sap_class):
        sap = fake_sap()
        sap_class.return_value = sap
        response = self.post(self.admin, f"{REQUESTS}direct-push/", NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["status"], "SAP_PUSHED")
        sap.create_product_tree.assert_called_once()
        self.assertEqual(sap.create_product_tree.call_args[0][0]["TreeCode"], "FG0000121")
        approval = BOMChangeApproval.objects.get()
        self.assertEqual((approval.level, approval.decided_by), (0, self.admin))
        self.assertTrue(response.data["approvals"][0]["direct"])
        self.assertEqual({s["state"] for s in response.data["steps"]}, {"skipped"})

    def test_sap_refusing_a_direct_push_leaves_no_request_behind(self, sap_class):
        """The portal inserted a PENDING row first and left it there on failure."""
        sap = fake_sap()
        sap.create_product_tree.side_effect = SAPValidationError("(-10) Invalid warehouse")
        sap_class.return_value = sap
        response = self.post(self.admin, f"{REQUESTS}direct-push/", NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "(-10) Invalid warehouse")
        self.assertFalse(BOMChangeRequest.objects.exists())
        self.assertFalse(BOMChangeLine.objects.exists())

    def test_a_direct_push_needs_the_direct_right(self, sap_class):
        sap_class.return_value = fake_sap()
        response = self.post(self.pusher, f"{REQUESTS}direct-push/", NEW_BOM)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap_class.return_value.create_product_tree.assert_not_called()


# ---------------------------------------------------------------------------
# SAP BOM viewer
# ---------------------------------------------------------------------------


@patch("bom_changes.views.SAPClient")
class ViewerTests(BomChangesTestCase):
    def test_search_reads_this_companys_sap(self, sap_class):
        sap_class.return_value.search_product_trees.return_value = [{"tree_code": "FG1"}]
        response = self.get(self.viewer, f"{BASE}sap-boms/?search=canola&limit=20")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"tree_code": "FG1"}])
        sap_class.assert_called_once_with(company_code="JIVO_OIL")
        sap_class.return_value.search_product_trees.assert_called_once_with("canola", limit=20)

    def test_one_tree_and_a_missing_one(self, sap_class):
        sap_class.return_value.get_product_tree.return_value = SAP_TREE
        response = self.get(self.viewer, f"{BASE}sap-boms/FG0000121/")
        self.assertEqual(response.data["lines"][0]["item_code"], "RM0001")
        sap_class.return_value.get_product_tree.return_value = None
        self.assertEqual(self.get(self.viewer, f"{BASE}sap-boms/NOPE/").status_code, status.HTTP_404_NOT_FOUND)

    def test_a_code_with_a_slash_reaches_sap_whole(self, sap_class):
        sap_class.return_value.get_product_tree.return_value = SAP_TREE
        self.get(self.viewer, f"{BASE}sap-boms/FG/01/")
        sap_class.return_value.get_product_tree.assert_called_once_with("FG/01")

    def test_sap_down_is_503(self, sap_class):
        sap_class.return_value.search_product_trees.side_effect = SAPConnectionError("Unable to connect to SAP HANA.")
        self.assertEqual(self.get(self.viewer, f"{BASE}sap-boms/").status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Rights and company scope
# ---------------------------------------------------------------------------


@patch("bom_changes.views.SAPClient")
@patch("bom_changes.services.SAPClient")
class PermissionTests(BomChangesTestCase):
    def setUp(self):
        super().setUp()
        with patch("bom_changes.services.SAPClient") as sap_class:
            self.pk = self.raise_request(sap_class)
        self.nobody = self.make_user("Nobody")

    def test_missing_company_header_is_refused(self, service_sap, view_sap):
        client = self.client_for(self.viewer)
        self.assertEqual(client.get(REQUESTS).status_code, status.HTTP_403_FORBIDDEN)

    def test_no_rights_is_refused_everywhere(self, service_sap, view_sap):
        gets = ["workflow/", "sap-boms/", "sap-boms/FG1/", "requests/", f"requests/{self.pk}/"]
        posts = ["requests/", "requests/direct-push/", f"requests/{self.pk}/approve/",
                 f"requests/{self.pk}/reject/", f"requests/{self.pk}/cancel/"]
        for path in gets:
            with self.subTest(get=path):
                self.assertEqual(self.get(self.nobody, f"{BASE}{path}").status_code, status.HTTP_403_FORBIDDEN)
        for path in posts:
            with self.subTest(post=path):
                self.assertEqual(self.post(self.nobody, f"{BASE}{path}", NEW_BOM).status_code,
                                 status.HTTP_403_FORBIDDEN)

    def test_viewers_read_but_do_not_write(self, service_sap, view_sap):
        view_sap.return_value.search_product_trees.return_value = []
        self.assertEqual(self.get(self.viewer, REQUESTS).status_code, status.HTTP_200_OK)
        self.assertEqual(self.get(self.viewer, f"{BASE}sap-boms/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.get(self.viewer, f"{BASE}workflow/").data["levels"], 3)
        for path in ("requests/", f"requests/{self.pk}/approve/", "requests/direct-push/"):
            with self.subTest(path=path):
                self.assertEqual(self.post(self.viewer, f"{BASE}{path}", NEW_BOM).status_code,
                                 status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post(self.viewer, f"{REQUESTS}{self.pk}/cancel/").status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_every_right_implies_viewing(self, service_sap, view_sap):
        for user in (self.requester, self.level_1, self.level_2, self.pusher,
                     self.make_user("Direct", "can_push_bom_directly")):
            with self.subTest(user=user.full_name):
                self.assertEqual(self.get(user, f"{REQUESTS}{self.pk}/").status_code, status.HTTP_200_OK)

    def test_another_companys_request_is_not_found(self, service_sap, view_sap):
        theirs = BOMChangeRequest.objects.create(
            company=self.other_company, kind="CREATE", item_code="FG9", item_name="Mart item"
        )
        both = self.make_user("Everything", *ALL_PERMISSIONS)
        self.assertEqual(self.get(both, f"{REQUESTS}{theirs.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        for action in ("approve", "reject", "cancel"):
            with self.subTest(action=action):
                self.assertEqual(self.post(both, f"{REQUESTS}{theirs.pk}/{action}/").status_code,
                                 status.HTTP_404_NOT_FOUND)
        self.assertNotIn(theirs.pk, [row["id"] for row in self.get(both, REQUESTS).data["results"]])
        self.assertEqual(BOMChangeRequest.objects.get(pk=theirs.pk).status, "PENDING")

    def test_a_member_of_another_company_only_is_refused(self, service_sap, view_sap):
        mart_user = self.make_user("Mart", *ALL_PERMISSIONS, company=self.other_company)
        self.assertEqual(self.get(mart_user, REQUESTS).status_code, status.HTTP_403_FORBIDDEN)


class PermissionSurfaceTests(TestCase):
    def test_only_the_declared_rights_exist(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="bom_changes").values_list("codename", flat=True)
        )
        self.assertEqual(codenames, set(ALL_PERMISSIONS))

    def test_the_guard_constants_name_those_rights(self):
        self.assertEqual({p.split(".", 1)[1] for p in guards.ALL_PERMISSIONS}, set(ALL_PERMISSIONS))


class GroupCommandTests(TestCase):
    """Every group right exists, and every right the API checks is handed out."""

    def setUp(self):
        call_command("setup_bom_changes_groups", stdout=StringIO())

    def test_every_group_is_created_with_its_rights(self):
        for name, codes in BOM_CHANGES_GROUPS.items():
            with self.subTest(group=name):
                held = {
                    f"bom_changes.{codename}"
                    for codename in Group.objects.get(name=name).permissions.values_list("codename", flat=True)
                }
                self.assertEqual(held, set(codes))

    def test_every_right_the_api_checks_is_granted_by_some_group(self):
        granted = {code for codes in BOM_CHANGES_GROUPS.values() for code in codes}
        self.assertEqual(set(guards.ALL_PERMISSIONS) - granted, set())

    def test_a_rerun_changes_nothing(self):
        before = {g.name: set(g.permissions.values_list("codename", flat=True)) for g in Group.objects.all()}
        call_command("setup_bom_changes_groups", stdout=StringIO())
        after = {g.name: set(g.permissions.values_list("codename", flat=True)) for g in Group.objects.all()}
        self.assertEqual(before, after)

    def test_list_prints_without_writing(self):
        Group.objects.all().delete()
        out = StringIO()
        call_command("setup_bom_changes_groups", "--list", stdout=out)
        self.assertIn("BOM Changes - SAP Pusher", out.getvalue())
        self.assertFalse(Group.objects.exists())
