"""Tests for the warehouse transfer guards.

These are the rules SAP enforces via `SBO_SP_TRANSACTIONNOTIFICATION`, restated
in the app so an operator gets a sentence instead of an error number. They are
pure logic — no SAP connection — so they run anywhere.

Branch layout used throughout mirrors production Oil: branch 1 is Delhi, 2 is
Bhakharpur, 3 is Punjab, and each has its own in-transit warehouse.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from .services import transfer_guards as guards
from .services.transfer_guards import TransferGuardError

BRANCHES = {
    "BH-PM": 2, "BH-BS": 2, "BH-WST": 2, "BH-PC": 2, "BH-LO": 2,
    "BH-GR": 2, "BH-BT": 2, "BH-INT": 2,
    "DL-PS": 1, "DL-FG": 1, "DL-INT": 1,
    "PB-PS": 3, "PB-INT": 3,
    "NO-BRANCH": None,
}


class ResolveRouteTests(SimpleTestCase):
    def test_same_branch_is_intra_branch(self):
        route = guards.resolve_route(
            from_warehouse="BH-PM", to_warehouse="BH-BS", branch_of=BRANCHES
        )
        self.assertFalse(route.is_cross_branch)
        self.assertEqual(route.from_branch_id, 2)
        self.assertEqual(route.intransit_warehouse, "")

    def test_cross_branch_picks_the_destination_branch_intransit(self):
        route = guards.resolve_route(
            from_warehouse="BH-BT", to_warehouse="PB-PS", branch_of=BRANCHES
        )
        self.assertTrue(route.is_cross_branch)
        # The in-transit warehouse belongs to the *destination* branch, which is
        # what SAP's rule requires.
        self.assertEqual(route.intransit_warehouse, "PB-INT")
        self.assertTrue(route.notes)

    def test_same_warehouse_is_refused(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.resolve_route(
                from_warehouse="BH-PM", to_warehouse="BH-PM", branch_of=BRANCHES
            )
        self.assertIn("does not move anything", str(ctx.exception))

    def test_missing_branch_is_refused_by_name(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.resolve_route(
                from_warehouse="NO-BRANCH", to_warehouse="BH-PM", branch_of=BRANCHES
            )
        self.assertIn("NO-BRANCH", str(ctx.exception))

    def test_unknown_warehouse_is_refused(self):
        with self.assertRaises(TransferGuardError):
            guards.resolve_route(
                from_warehouse="BH-PM", to_warehouse="ZZ-NOPE", branch_of=BRANCHES
            )

    def test_blank_warehouse_is_refused(self):
        with self.assertRaises(TransferGuardError):
            guards.resolve_route(
                from_warehouse="", to_warehouse="BH-PM", branch_of=BRANCHES
            )


class CheckRouteTests(SimpleTestCase):
    def _route(self, frm, to):
        return guards.resolve_route(
            from_warehouse=frm, to_warehouse=to, branch_of=BRANCHES
        )

    def test_cross_branch_direct_to_destination_is_refused(self):
        route = self._route("BH-BT", "PB-PS")
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_route(
                from_warehouse="BH-BT", to_warehouse="PB-PS", route=route
            )
        self.assertIn("5900002", str(ctx.exception))

    def test_cross_branch_into_intransit_is_allowed(self):
        route = self._route("BH-BT", "PB-PS")
        guards.check_route(
            from_warehouse="BH-BT", to_warehouse="PB-INT", route=route
        )

    def test_transfers_into_goods_return_are_refused(self):
        route = self._route("BH-PM", "BH-GR")
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_route(
                from_warehouse="BH-PM", to_warehouse="BH-GR", route=route
            )
        self.assertIn("670851", str(ctx.exception))

    def test_goods_return_from_intransit_is_allowed(self):
        # The one exemption SAP grants, since the app posts as B1i (user 2) and
        # the UserSign 52 carve-out never applies to us.
        route = guards.RouteDecision(False, 2, 2, "BH-INT")
        guards.check_route(
            from_warehouse="BH-INT", to_warehouse="BH-GR", route=route
        )

    def test_second_leg_must_start_from_intransit(self):
        route = guards.RouteDecision(False, 3, 3, "PB-INT")
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_route(
                from_warehouse="BH-BT", to_warehouse="PB-PS",
                route=route, is_second_leg=True,
            )
        self.assertIn("in-transit", str(ctx.exception))

    def test_second_leg_may_not_cross_branches(self):
        route = guards.RouteDecision(False, 3, 1, "PB-INT")
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_route(
                from_warehouse="PB-INT", to_warehouse="DL-PS",
                route=route, is_second_leg=True,
            )
        self.assertIn("6700001", str(ctx.exception))

    def test_valid_second_leg_passes(self):
        route = guards.RouteDecision(False, 3, 3, "PB-INT")
        guards.check_route(
            from_warehouse="PB-INT", to_warehouse="PB-PS",
            route=route, is_second_leg=True,
        )


class CheckLinesTests(SimpleTestCase):
    def _line(self, **kwargs):
        line = {
            "item_code": "PM0000019",
            "quantity": Decimal("10"),
            "from_warehouse": "BH-PM",
            "to_warehouse": "BH-BS",
        }
        line.update(kwargs)
        return line

    def test_plain_line_passes(self):
        guards.check_lines(
            lines=[self._line()],
            batch_flags={"PM0000019": False},
            has_transfer_request=False,
        )

    def test_no_lines_is_refused(self):
        with self.assertRaises(TransferGuardError):
            guards.check_lines(lines=[], batch_flags={}, has_transfer_request=False)

    def test_zero_quantity_is_refused(self):
        with self.assertRaises(TransferGuardError):
            guards.check_lines(
                lines=[self._line(quantity=Decimal("0"))],
                batch_flags={}, has_transfer_request=False,
            )

    def test_loose_oil_needs_a_request(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_lines(
                lines=[self._line(from_warehouse="BH-LO", to_warehouse="BH-PC")],
                batch_flags={"PM0000019": False},
                has_transfer_request=False,
            )
        self.assertIn("67081", str(ctx.exception))

    def test_loose_oil_passes_with_a_request(self):
        guards.check_lines(
            lines=[self._line(from_warehouse="BH-LO", to_warehouse="BH-PC")],
            batch_flags={"PM0000019": False},
            has_transfer_request=True,
        )

    def test_batch_managed_line_without_batches_is_refused(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_lines(
                lines=[self._line(item_code="FG0000142")],
                batch_flags={"FG0000142": True},
                has_transfer_request=False,
            )
        self.assertIn("batch-managed", str(ctx.exception))

    def test_batch_split_must_match_the_line_quantity(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_lines(
                lines=[self._line(
                    item_code="FG0000142",
                    quantity=Decimal("10"),
                    batches=[{"BatchNumber": "A", "Quantity": 6}],
                )],
                batch_flags={"FG0000142": True},
                has_transfer_request=False,
            )
        self.assertIn("adds up to 6", str(ctx.exception))

    def test_matching_batch_split_passes(self):
        guards.check_lines(
            lines=[self._line(
                item_code="FG0000142",
                quantity=Decimal("10"),
                batches=[
                    {"BatchNumber": "A", "Quantity": 6},
                    {"BatchNumber": "B", "Quantity": 4},
                ],
            )],
            batch_flags={"FG0000142": True},
            has_transfer_request=False,
        )


class WholeUnitTests(SimpleTestCase):
    """A fractional count of a discrete item is nonsense, and SAP allows it.

    Live proof: transfer 826676784 posted 0.993 PCS of FG0000005 and SAP raised
    nothing. Since nothing downstream objects, this is the only place it can be
    caught — which is why it is a refusal rather than a warning.
    """

    def test_fraction_of_a_piece_is_refused(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_whole_units('FG0000005', Decimal('0.993'), 'PCS')
        self.assertIn('whole', str(ctx.exception))
        self.assertIn('0.993', str(ctx.exception))

    def test_whole_pieces_pass(self):
        guards.check_whole_units('FG0000005', Decimal('1'), 'PCS')
        guards.check_whole_units('FG0000005', Decimal('2000.000'), 'PCS')

    def test_other_discrete_units_are_covered(self):
        for uom in ('NOS', 'SET', 'DRM', 'pcs', ' PCS '):
            with self.assertRaises(TransferGuardError):
                guards.check_whole_units('X', Decimal('1.5'), uom)

    def test_measured_units_may_be_fractional(self):
        # Loose oil in litres and raw material in kilos are genuinely fractional.
        for uom in ('LTR', 'KGS', 'GMS', 'MTR', 'MTS'):
            guards.check_whole_units('RM0000001', Decimal('17.005'), uom)

    def test_unknown_or_blank_unit_is_left_alone(self):
        # Guessing at an unrecognised unit would block legitimate transfers.
        guards.check_whole_units('X', Decimal('1.5'), '')
        guards.check_whole_units('X', Decimal('1.5'), 'WIDGETS')

    def test_check_lines_enforces_it_when_the_unit_is_known(self):
        with self.assertRaises(TransferGuardError):
            guards.check_lines(
                lines=[{
                    'item_code': 'FG0000005', 'quantity': Decimal('0.993'),
                    'from_warehouse': 'BH-BT', 'to_warehouse': 'BH-BS', 'uom': 'PCS',
                }],
                batch_flags={'FG0000005': False},
                has_transfer_request=False,
            )

    def test_check_lines_skips_it_when_the_unit_is_absent(self):
        # Older callers that do not carry a unit must keep working.
        guards.check_lines(
            lines=[{
                'item_code': 'FG0000005', 'quantity': Decimal('0.993'),
                'from_warehouse': 'BH-BT', 'to_warehouse': 'BH-BS',
            }],
            batch_flags={'FG0000005': False},
            has_transfer_request=False,
        )


class PostingDateTests(SimpleTestCase):
    def test_date_before_the_sap_lock_is_refused(self):
        with self.assertRaises(TransferGuardError) as ctx:
            guards.check_posting_date(date(2025, 8, 17))
        self.assertIn("67001081", str(ctx.exception))

    def test_date_on_the_boundary_passes(self):
        guards.check_posting_date(date(2025, 8, 18))

    def test_today_passes(self):
        guards.check_posting_date(date.today())


class _Manager:
    """Stands in for a related manager so these stay DB-free."""

    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return self._items


class _Line:
    def __init__(self, line_num, item_code, transferred_qty):
        self.line_num = line_num
        self.item_code = item_code
        self.transferred_qty = Decimal(str(transferred_qty))


class _Scan:
    def __init__(self, item_code, quantity, receive_status):
        self.item_code = item_code
        self.quantity = Decimal(str(quantity))
        self.receive_status = receive_status


class _Request:
    def __init__(self, lines):
        self.lines = _Manager(lines)


class _BST:
    def __init__(self, scans):
        self.box_scans = _Manager(scans)


class ReceivedQuantitiesTests(SimpleTestCase):
    """How a receipt turns into the quantity leg 2 posts.

    The distinction that matters: an item whose boxes were all *rejected*
    settles at zero, while an item that was never scannable at all (packaging
    material) falls back to what leg 1 moved. Collapsing those two cases would
    either strand PM in the in-transit warehouse or silently move rejected
    stock onward.
    """

    def setUp(self):
        from .services.transfer_request_service import TransferRequestService
        from .models_bst import BSTReceiveStatus
        self.service = TransferRequestService("JIVO_OIL")
        self.ACCEPTED = BSTReceiveStatus.ACCEPTED
        self.REJECTED = BSTReceiveStatus.REJECTED
        self.PENDING = BSTReceiveStatus.PENDING

    def test_all_boxes_accepted_settles_at_the_scanned_total(self):
        request = _Request([_Line(0, "FG0000142", 100)])
        bst = _BST([
            _Scan("FG0000142", 60, self.ACCEPTED),
            _Scan("FG0000142", 40, self.ACCEPTED),
        ])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("100")},
        )

    def test_rejected_boxes_are_excluded(self):
        request = _Request([_Line(0, "FG0000142", 100)])
        bst = _BST([
            _Scan("FG0000142", 60, self.ACCEPTED),
            _Scan("FG0000142", 40, self.REJECTED),
        ])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("60")},
        )

    def test_pending_boxes_are_excluded(self):
        request = _Request([_Line(0, "FG0000142", 100)])
        bst = _BST([
            _Scan("FG0000142", 70, self.ACCEPTED),
            _Scan("FG0000142", 30, self.PENDING),
        ])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("70")},
        )

    def test_scan_exempt_line_falls_back_to_what_leg_one_moved(self):
        # Packaging material is never barcode-scanned, so it has no scans at all.
        request = _Request([
            _Line(0, "FG0000142", 100),
            _Line(1, "PM0000019", 500),
        ])
        bst = _BST([_Scan("FG0000142", 100, self.ACCEPTED)])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("100"), 1: Decimal("500")},
        )

    def test_fully_rejected_item_settles_at_zero_not_the_fallback(self):
        request = _Request([_Line(0, "FG0000142", 100)])
        bst = _BST([_Scan("FG0000142", 100, self.REJECTED)])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("0")},
        )

    def test_short_receipt_leaves_the_remainder_behind(self):
        # 40 of 100 accepted: leg 2 moves 40 and the other 60 stays in the
        # in-transit warehouse, which is where in-transit shortfall belongs.
        request = _Request([_Line(0, "FG0000142", 100)])
        bst = _BST([
            _Scan("FG0000142", 40, self.ACCEPTED),
            _Scan("FG0000142", 60, self.REJECTED),
        ])
        self.assertEqual(
            self.service.received_quantities_from_bst(request, bst),
            {0: Decimal("40")},
        )


class _ReconLine:
    def __init__(self, transferred_qty):
        self.transferred_qty = Decimal(str(transferred_qty))


class _ReconRequest:
    """Enough of a request for the reconciler, without a database."""

    def __init__(self, **kw):
        from .models_transfer import (
            TransferPostingStatus, TransferRequestStatus, TransferRouteType,
        )
        from django.utils import timezone as tz

        self.id = kw.get("id", 1)
        self.entry_no = kw.get("entry_no", "TR-20260826-0001")
        self.created_at = kw.get("created_at", tz.now() - tz.timedelta(days=10))
        self.status = kw.get("status", TransferRequestStatus.APPROVED)
        self.posting_status = kw.get("posting_status", TransferPostingStatus.POSTED)
        self.posting_error = kw.get("posting_error", "")
        self.route_type = kw.get("route_type", TransferRouteType.INTRA_BRANCH)
        self.intransit_warehouse = kw.get("intransit_warehouse", "")
        self.posted_at = kw.get("posted_at", tz.now())
        self.reviewed_at = kw.get("reviewed_at", tz.now())
        self.sap_request_doc_entry = kw.get("sap_request_doc_entry", 2681)
        self.sap_request_doc_num = kw.get("sap_request_doc_num", "826656533")
        self.sap_transfer_doc_entry = kw.get("sap_transfer_doc_entry", 21525)
        self.bst_transfer_id = kw.get("bst_transfer_id", 99)
        self.lines = _Manager(kw.get("lines", [_ReconLine(100)]))
        self._status_display = kw.get("status_display", str(self.status).title())

    def get_status_display(self):
        return self._status_display

    @property
    def is_approved(self):
        from .models_transfer import TransferRequestStatus
        return self.status in (
            TransferRequestStatus.APPROVED,
            TransferRequestStatus.PARTIALLY_APPROVED,
        )


def _sap(**kw):
    total = Decimal(str(kw.get("total_quantity", 100)))
    open_qty = Decimal(str(kw.get("open_quantity", 0)))
    return {
        "doc_status": kw.get("doc_status", "C"),
        "cancelled": kw.get("cancelled", False),
        "line_count": kw.get("line_count", 1),
        "open_lines": kw.get("open_lines", 0),
        "total_quantity": total,
        "open_quantity": open_qty,
        "served_quantity": kw.get("served_quantity", total - open_qty),
        "age_days": kw.get("age_days", 10),
        "is_open": kw.get("is_open", False),
    }


class ReconciliationTests(SimpleTestCase):
    """Each finding is a way app and SAP can disagree about where stock is."""

    def _codes(self, request, sap):
        from .services.transfer_reconciliation import TransferReconciler
        return {f.code for f in TransferReconciler(None)._check(request, sap)}

    def test_a_settled_request_reports_nothing(self):
        self.assertEqual(self._codes(_ReconRequest(), _sap()), set())

    def test_rejected_but_still_reserved_is_critical(self):
        from .models_transfer import TransferRequestStatus, TransferPostingStatus
        from .services.transfer_reconciliation import TransferReconciler

        request = _ReconRequest(
            status=TransferRequestStatus.REJECTED,
            posting_status=TransferPostingStatus.NOT_POSTED,
            sap_transfer_doc_entry=None, bst_transfer_id=None,
            lines=[_ReconLine(0)],
        )
        sap = _sap(is_open=True, open_lines=1, open_quantity=100,
                   served_quantity=0, doc_status="O")
        findings = TransferReconciler(None)._check(request, sap)
        match = [f for f in findings if f.code == "reservation_not_released"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].severity, "critical")

    def test_missing_sap_request_short_circuits(self):
        # No point reporting anything else about a document SAP has lost.
        codes = self._codes(_ReconRequest(), None)
        self.assertEqual(codes, {"sap_request_missing"})

    def test_request_never_mirrored_is_flagged(self):
        request = _ReconRequest(sap_request_doc_entry=None, sap_request_doc_num="")
        self.assertIn("no_sap_request", self._codes(request, None))

    def test_reservation_left_open_after_posting(self):
        sap = _sap(is_open=True, open_lines=1, open_quantity=40, served_quantity=60)
        codes = self._codes(_ReconRequest(lines=[_ReconLine(60)]), sap)
        self.assertIn("reservation_open_after_posting", codes)

    def test_sap_closed_while_app_still_pending(self):
        from .models_transfer import TransferRequestStatus, TransferPostingStatus
        request = _ReconRequest(
            status=TransferRequestStatus.PENDING,
            posting_status=TransferPostingStatus.NOT_POSTED,
            sap_transfer_doc_entry=None, bst_transfer_id=None,
            lines=[_ReconLine(0)],
        )
        codes = self._codes(request, _sap(served_quantity=0))
        self.assertIn("sap_closed_while_pending", codes)

    def test_quantity_mismatch_is_reported(self):
        # App thinks 100 moved, SAP served 60.
        sap = _sap(served_quantity=60, open_quantity=0)
        codes = self._codes(_ReconRequest(lines=[_ReconLine(100)]), sap)
        self.assertIn("quantity_mismatch", codes)

    def test_rejected_and_closed_is_not_a_quantity_mismatch(self):
        # SAP derives "served" as total minus open, and Close zeroes OpenQty —
        # so a rejected request reads as fully served. Without the posting-status
        # gate this fired a false mismatch on every single rejection.
        from .models_transfer import TransferRequestStatus, TransferPostingStatus
        request = _ReconRequest(
            status=TransferRequestStatus.REJECTED,
            posting_status=TransferPostingStatus.NOT_POSTED,
            sap_transfer_doc_entry=None, bst_transfer_id=None,
            lines=[_ReconLine(0)],
        )
        # Closed unserved: open is 0, so served reads as the full 100.
        sap = _sap(total_quantity=100, open_quantity=0, served_quantity=100,
                   is_open=False, doc_status="C")
        self.assertEqual(self._codes(request, sap), set())

    def test_mismatch_still_caught_once_posted(self):
        from .models_transfer import TransferPostingStatus
        request = _ReconRequest(
            posting_status=TransferPostingStatus.POSTED,
            lines=[_ReconLine(100)],
        )
        codes = self._codes(request, _sap(served_quantity=60, open_quantity=0))
        self.assertIn("quantity_mismatch", codes)

    def test_failed_posting_is_critical(self):
        from .models_transfer import TransferPostingStatus
        from .services.transfer_reconciliation import TransferReconciler
        request = _ReconRequest(
            posting_status=TransferPostingStatus.FAILED,
            posting_error="Inventory Transfer blocked (SAP 5900002)",
            sap_transfer_doc_entry=None, bst_transfer_id=None,
            lines=[_ReconLine(0)],
        )
        findings = TransferReconciler(None)._check(request, _sap(served_quantity=0))
        match = [f for f in findings if f.code == "posting_failed"]
        self.assertEqual(match[0].severity, "critical")
        self.assertIn("5900002", match[0].message)

    def test_stuck_in_transit_is_critical(self):
        from django.utils import timezone as tz
        from .models_transfer import TransferPostingStatus, TransferRouteType
        from .services.transfer_reconciliation import (
            STUCK_IN_TRANSIT_DAYS, TransferReconciler,
        )
        request = _ReconRequest(
            route_type=TransferRouteType.CROSS_BRANCH,
            posting_status=TransferPostingStatus.IN_TRANSIT,
            intransit_warehouse="PB-INT",
            posted_at=tz.now() - tz.timedelta(days=STUCK_IN_TRANSIT_DAYS + 1),
        )
        findings = TransferReconciler(None)._check(request, _sap())
        match = [f for f in findings if f.code == "stuck_in_transit"]
        self.assertEqual(match[0].severity, "critical")
        self.assertIn("PB-INT", match[0].message)

    def test_recently_dispatched_cross_branch_is_not_stuck(self):
        from django.utils import timezone as tz
        from .models_transfer import TransferPostingStatus, TransferRouteType
        request = _ReconRequest(
            route_type=TransferRouteType.CROSS_BRANCH,
            posting_status=TransferPostingStatus.IN_TRANSIT,
            intransit_warehouse="PB-INT",
            posted_at=tz.now() - tz.timedelta(days=1),
        )
        self.assertNotIn("stuck_in_transit", self._codes(request, _sap()))

    def test_approved_but_never_posted_is_flagged(self):
        from django.utils import timezone as tz
        from .models_transfer import TransferPostingStatus
        from .services.transfer_reconciliation import AWAITING_POST_DAYS
        request = _ReconRequest(
            posting_status=TransferPostingStatus.NOT_POSTED,
            reviewed_at=tz.now() - tz.timedelta(days=AWAITING_POST_DAYS + 1),
            sap_transfer_doc_entry=None, bst_transfer_id=None,
            lines=[_ReconLine(0)],
        )
        codes = self._codes(request, _sap(is_open=True, open_lines=1,
                                          open_quantity=100, served_quantity=0))
        self.assertIn("approved_but_not_posted", codes)

    def test_posted_without_a_bst_is_flagged(self):
        request = _ReconRequest(bst_transfer_id=None)
        self.assertIn("no_bst", self._codes(request, _sap()))

    def test_findings_are_ordered_worst_first(self):
        from .services.transfer_reconciliation import Finding, SEVERITY_ORDER
        findings = [
            Finding("A", 1, "info", "c1", "m"),
            Finding("B", 2, "critical", "c2", "m"),
            Finding("C", 3, "warning", "c3", "m"),
        ]
        findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.entry_no))
        self.assertEqual(
            [f.severity for f in findings], ["critical", "warning", "info"]
        )


class CardCodeTests(SimpleTestCase):
    def test_wastage_route_gets_the_mandated_card(self):
        self.assertEqual(
            guards.card_code_for_route("BH-PC", "BH-GR"), "CUSTA000940"
        )

    def test_other_routes_get_no_card(self):
        # Setting the wastage card on any other route is itself a SAP rejection,
        # which is why this returns per route rather than being caller-supplied.
        self.assertEqual(guards.card_code_for_route("BH-PM", "BH-BS"), "")
