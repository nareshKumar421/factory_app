"""The -5002 recovery path: ask about the GRPO SAP already holds, then adopt it.

SAP refuses a second document with the same (vendor, NumAtCard), so once a bilty's
freight is booked there is no post that can succeed -- and the app routinely ends
up in exactly that state, because ``post_service_grpo`` is atomic while SAP's
commit is not: anything raising after ``create_grpo`` returns rolls our POSTED row
back and leaves the SAP document standing. Beverages bill 626088199 / bilty 1840
sat in the queue for two days that way, retrying a post SAP would never take.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from grpo.models import GRPOStatus, ServiceGRPOPosting
from grpo.services import GRPOService, ServiceGRPOAlreadyInSAP
from sap_client.exceptions import SAPValidationError

User = get_user_model()

DUPLICATE_REFERENCE_ERROR = (
    '{"error":{"code":"-5002","message":"10001467 - There is already a record '
    'with duplicated customer/vendor reference number."}}'
)

# What SAP actually held for bilty 1840 while the queue kept offering it.
SAP_DOC = {
    "doc_entry": 10249,
    "doc_num": 2026088268,
    "doc_date": "2026-08-24",
    "doc_total": "5167.00",
    "doc_type": "S",
    "card_code": "VENDA001259",
    "card_name": "BHARGAVE ROAD CARRIER",
    "vendor_ref": "1840",
    "comments": "BILTY NO 1840",
    "sap_absolute_entry": 41894,
    "can_adopt": True,
}


class ServiceGRPOAlreadyInSAPTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Bev", code="JIVO_BEV_ADOPT")
        self.user = User.objects.create_user(
            email="adopt@example.com",
            password="p",
            full_name="Adopt Tester",
            employee_code="ADOPT1",
        )
        self.plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626088199,
            sap_invoice_doc_num="626088199",
            booking_status=DispatchPlanStatus.DISPATCHED,
            dispatch_date=date(2026, 8, 24),
            place_of_supply="DL",
            total_freight=Decimal("5167.00"),
            bilty_no="1840",
            bilty_date=date(2026, 8, 24),
        )
        self.service = GRPOService(company_code=self.company.code)
        self.sap_client = MagicMock()

    # ------------------------------------------------------------------ #
    # harness
    # ------------------------------------------------------------------ #
    def _run(self, *, existing, adopt=False, create_grpo=None, bp_group_code=None):
        """Post once, with SAP's answer about an existing document pinned.

        ``existing`` is the document ``find_existing_sap_service_grpo`` reports —
        or a list, to give the pre-flight and the post-failure re-check different
        answers. ``bp_group_code`` drives the attachment pre-check.
        """
        sap_reads = {
            "_get_sap_branch_states": {2: "DL"},
            "_get_sap_bp_state": "DL",
            "_get_active_dimension_codes": None,
            "_get_sap_bp_group_code": bp_group_code,
            "_get_sap_tax_codes": {
                "GST05R": {"code": "GST05R", "name": "RCM", "rate": Decimal("5")}
            },
            "_get_dispatch_bill_snapshot": {
                "doc_num": "626088199",
                "state": "DL",
                "card_code": "ORGC000016",
                "item_summary": "WATER",
                "total_litres": "1000.000",
                "doc_total": "5167.00",
            },
            # Payload-only; keeps the test off HANA's UDF introspection.
            "_filter_purchase_delivery_note_udfs": None,
        }
        patches = [
            patch.object(GRPOService, target, return_value=value)
            for target, value in sap_reads.items()
        ]
        patches.append(patch.object(GRPOService, "find_existing_sap_service_grpo"))
        patches.append(patch("grpo.services.SAPClient"))

        started = [p.start() for p in patches]
        try:
            finder, sap_client_cls = started[-2], started[-1]
            if isinstance(existing, list):
                finder.side_effect = existing
            else:
                finder.return_value = existing

            self.sap_client = MagicMock()
            if create_grpo is not None:
                self.sap_client.create_grpo.side_effect = create_grpo
            else:
                self.sap_client.create_grpo.return_value = {
                    "DocEntry": 99999,
                    "DocNum": 2026099999,
                    "DocTotal": 5167.00,
                }
            sap_client_cls.return_value = self.sap_client

            return self.service.post_service_grpo(
                dispatch_plan_id=self.plan.id,
                user=self.user,
                vendor_code="VENDA001259",
                branch_id=2,
                service_description="Transport freight",
                amount=Decimal("5167.00"),
                tax_code="GST05R",
                gl_account="5670001",
                place_of_supply="DL",
                effective_month="2026-08",
                location_code=2,
                location_name="DELHI",
                sac_entry=40,
                sac_code="9965",
                vendor_ref="1840",
                include_bilty_attachment=False,
                adopt_existing_sap_doc=adopt,
            )
        finally:
            for p in reversed(patches):
                p.stop()

    def _pending_plan_ids(self):
        return [
            plan.id
            for plan in self.service.get_pending_service_grpo_entries(year=2026, month=8)
        ]

    # ------------------------------------------------------------------ #
    # ask, don't retry
    # ------------------------------------------------------------------ #
    def test_a_bilty_sap_already_holds_is_offered_instead_of_posted(self):
        with self.assertRaises(ServiceGRPOAlreadyInSAP) as caught:
            self._run(existing=SAP_DOC)

        self.assertEqual(caught.exception.sap_doc["doc_num"], 2026088268)
        self.assertIn("2026088268", str(caught.exception))
        # Nothing sent, and no row left behind to muddy the history.
        self.sap_client.create_grpo.assert_not_called()
        self.assertFalse(ServiceGRPOPosting.objects.exists())

    def test_the_offer_comes_before_the_form_validations(self):
        """The pre-flight runs ahead of the other checks, deliberately.

        A plan missing something a real post needs -- here the attachment SAP
        demands of this vendor group -- must still reach the adopt offer. Reporting
        the missing attachment first sends the operator off to fix a form for a
        post that could never go through anyway.
        """
        with self.assertRaises(ServiceGRPOAlreadyInSAP):
            self._run(existing=SAP_DOC, bp_group_code=100)

    def test_a_blank_vendor_reference_is_never_looked_up(self):
        """Without a NumAtCard there is no duplicate for SAP to block on.

        The lookup short-circuits rather than running a pointless HANA query on
        every post that leaves the vendor reference empty.
        """
        with patch("grpo.services.HanaConnection") as hana:
            self.assertIsNone(
                self.service.find_existing_sap_service_grpo("VENDA001259", "")
            )
            self.assertIsNone(
                self.service.find_existing_sap_service_grpo("VENDA001259", "   ")
            )
            self.assertIsNone(self.service.find_existing_sap_service_grpo("", "1840"))
            hana.assert_not_called()

    def test_an_unreadable_hana_never_grounds_a_posting(self):
        """A failed lookup must fall through and let SAP stay the authority.

        Otherwise an unreachable HANA turns into "cannot post anything", which is
        strictly worse than the -5002 this check exists to pre-empt.
        """
        with patch(
            "grpo.services.HanaConnection", side_effect=OSError("no route to host")
        ), patch("grpo.services.CompanyContext"):
            self.assertIsNone(
                self.service.find_existing_sap_service_grpo("VENDA001259", "1840")
            )

    # ------------------------------------------------------------------ #
    # adopt
    # ------------------------------------------------------------------ #
    def test_adopting_records_the_sap_document_without_posting_anything(self):
        posting = self._run(existing=SAP_DOC, adopt=True)

        self.sap_client.create_grpo.assert_not_called()
        self.assertEqual(posting.status, GRPOStatus.POSTED)
        self.assertEqual(posting.sap_doc_entry, 10249)
        self.assertEqual(posting.sap_doc_num, 2026088268)
        self.assertEqual(posting.sap_doc_total, Decimal("5167.00"))
        self.assertIsNotNone(posting.posted_at)
        self.assertEqual(posting.posted_by, self.user)

    def test_an_adopted_posting_carries_its_lines_like_a_posted_one(self):
        """Adopted and posted bookings have to be indistinguishable downstream."""
        posting = self._run(existing=SAP_DOC, adopt=True)

        lines = list(posting.lines.all())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].dispatch_plan_id, self.plan.id)
        self.assertEqual(lines[0].amount, Decimal("5167.00"))
        self.assertEqual(lines[0].gl_account, "5670001")
        self.assertEqual(lines[0].sac_code, "9965")

    def test_an_adopted_bilty_leaves_the_pending_queue(self):
        """The whole point: the row stops being offered for posting."""
        self.assertIn(self.plan.id, self._pending_plan_ids())

        self._run(existing=SAP_DOC, adopt=True)

        self.assertNotIn(self.plan.id, self._pending_plan_ids())

    # ------------------------------------------------------------------ #
    # refuse to guess
    # ------------------------------------------------------------------ #
    def test_adopting_a_document_that_is_no_longer_there_posts_nothing(self):
        """Confirming an adopt must never fall through into a fresh post.

        The operator agreed to record ONE specific document. If it cannot be found
        -- cancelled, or HANA unreadable -- the honest outcome is to stop; posting
        a new GRPO is the opposite of what they approved.
        """
        with self.assertRaisesMessage(ValueError, "could no longer be found"):
            self._run(existing=None, adopt=True)

        self.sap_client.create_grpo.assert_not_called()
        self.assertFalse(ServiceGRPOPosting.objects.exists())

    def test_a_document_that_is_not_a_service_grpo_cannot_be_adopted(self):
        item_grpo = {**SAP_DOC, "doc_type": "I", "can_adopt": False}

        with self.assertRaisesMessage(ValueError, "not a service"):
            self._run(existing=item_grpo, adopt=True)

        self.sap_client.create_grpo.assert_not_called()
        self.assertFalse(ServiceGRPOPosting.objects.exists())

    # ------------------------------------------------------------------ #
    # the late rejection
    # ------------------------------------------------------------------ #
    def test_a_duplicate_sap_rejects_late_is_still_offered_for_adoption(self):
        """The pre-flight can miss it: HANA down, or another post won the race.

        SAP's own -5002 then has to be turned back into the same question rather
        than surfacing as a dead-end validation error.
        """
        with self.assertRaises(ServiceGRPOAlreadyInSAP) as caught:
            self._run(
                # None on the pre-flight, found on the post-failure re-check.
                existing=[None, SAP_DOC],
                create_grpo=SAPValidationError(DUPLICATE_REFERENCE_ERROR),
            )

        self.assertEqual(caught.exception.sap_doc["doc_num"], 2026088268)
        self.assertFalse(ServiceGRPOPosting.objects.exists())

    def test_an_unrelated_sap_rejection_is_still_reported_as_a_failure(self):
        """Only the duplicate-reference rejection becomes a question."""
        with self.assertRaises(SAPValidationError):
            self._run(
                existing=[None, None],
                create_grpo=SAPValidationError("(200019) Please Attach its Receiving"),
            )

    def test_a_duplicate_with_no_document_found_stays_a_failure(self):
        """No document to offer means there is nothing to ask about."""
        with self.assertRaises(SAPValidationError):
            self._run(
                existing=[None, None],
                create_grpo=SAPValidationError(DUPLICATE_REFERENCE_ERROR),
            )
