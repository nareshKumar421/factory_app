"""Tests for what the app counts as its own hold on a warehouse's stock.

The picker used to net SAP's `OITW."IsCommited"` off on-hand, which handed every
warehouse the sum of everybody's open paperwork — including transfer requests
keyed by hand in 2024 and never closed. This module replaces that with the app's
own open requests, so what matters is exactly which of our requests still expect
to take stock out, and for how much.

Needs the database, so these are `TestCase` rather than the DB-free guard tests.
"""

from decimal import Decimal

from company.models import Company
from django.test import TestCase

from .models_transfer import (
    TransferLineStatus,
    TransferPostingStatus,
    TransferRequestStatus,
    TransferRouteType,
    WarehouseTransferRequest,
    WarehouseTransferRequestLine,
)
from .services.transfer_reservations import reserved_by_open_requests

ITEM = "FG0000011"
SOURCE = "BH-PF"


class ReservedByOpenRequestsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")

    # --- helpers -------------------------------------------------------

    def _request(self, *, company=None, **kwargs):
        defaults = dict(
            company=company or self.company,
            entry_no=WarehouseTransferRequest.generate_entry_no(),
            from_warehouse=SOURCE,
            to_warehouse="BH-BT",
            route_type=TransferRouteType.INTRA_BRANCH,
            from_branch_id=2,
            to_branch_id=2,
            status=TransferRequestStatus.PENDING,
            posting_status=TransferPostingStatus.NOT_POSTED,
        )
        defaults.update(kwargs)
        return WarehouseTransferRequest.objects.create(**defaults)

    def _line(self, request, *, item_code=ITEM, requested="100", **kwargs):
        defaults = dict(
            request=request,
            line_num=request.lines.count(),
            item_code=item_code,
            requested_qty=Decimal(requested),
        )
        defaults.update(kwargs)
        return WarehouseTransferRequestLine.objects.create(**defaults)

    def _reserved(self, warehouse=SOURCE, **kwargs):
        return reserved_by_open_requests("JIVO_OIL", warehouse, **kwargs)

    # --- what holds stock ----------------------------------------------

    def test_pending_request_holds_what_it_asked_for(self):
        self._line(self._request(), requested="160")
        self.assertEqual(self._reserved(), {ITEM: Decimal("160")})

    def test_two_open_requests_add_up(self):
        self._line(self._request(), requested="160")
        self._line(self._request(), requested="40")
        self.assertEqual(self._reserved(), {ITEM: Decimal("200")})

    def test_approved_quantity_wins_once_the_receiver_has_decided(self):
        request = self._request(status=TransferRequestStatus.APPROVED)
        self._line(request, requested="160", approved_qty=Decimal("100"))
        self.assertEqual(self._reserved(), {ITEM: Decimal("100")})

    def test_a_partly_moved_line_holds_only_the_remainder(self):
        request = self._request(status=TransferRequestStatus.APPROVED)
        self._line(
            request, requested="160",
            approved_qty=Decimal("160"), transferred_qty=Decimal("60"),
        )
        self.assertEqual(self._reserved(), {ITEM: Decimal("100")})

    def test_partially_approved_requests_still_hold(self):
        request = self._request(status=TransferRequestStatus.PARTIALLY_APPROVED)
        self._line(request, requested="160", approved_qty=Decimal("60"))
        self.assertEqual(self._reserved(), {ITEM: Decimal("60")})

    # --- what does not --------------------------------------------------

    def test_posted_request_holds_nothing_because_the_stock_has_gone(self):
        # On-hand already reflects the move; subtracting again double-counts.
        request = self._request(
            status=TransferRequestStatus.APPROVED,
            posting_status=TransferPostingStatus.POSTED,
        )
        self._line(request, requested="160", approved_qty=Decimal("160"))
        self.assertEqual(self._reserved(), {})

    def test_in_transit_request_holds_nothing(self):
        # Leg 1 of a cross-branch move has already taken the stock out.
        request = self._request(
            status=TransferRequestStatus.APPROVED,
            route_type=TransferRouteType.CROSS_BRANCH,
            posting_status=TransferPostingStatus.IN_TRANSIT,
        )
        self._line(request, requested="160", approved_qty=Decimal("160"))
        self.assertEqual(self._reserved(), {})

    def test_rejected_and_cancelled_requests_hold_nothing(self):
        self._line(self._request(status=TransferRequestStatus.REJECTED))
        self._line(self._request(status=TransferRequestStatus.CANCELLED))
        self.assertEqual(self._reserved(), {})

    def test_a_rejected_line_holds_nothing_even_on_a_live_request(self):
        request = self._request(status=TransferRequestStatus.PARTIALLY_APPROVED)
        self._line(request, requested="160", status=TransferLineStatus.REJECTED)
        self._line(request, requested="40", approved_qty=Decimal("40"))
        self.assertEqual(self._reserved(), {ITEM: Decimal("40")})

    def test_a_failed_posting_still_holds_because_it_will_be_retried(self):
        request = self._request(posting_status=TransferPostingStatus.FAILED)
        self._line(request, requested="160")
        self.assertEqual(self._reserved(), {ITEM: Decimal("160")})

    # --- scoping ---------------------------------------------------------

    def test_another_warehouse_is_not_counted(self):
        self._line(self._request(from_warehouse="BH-BT"), requested="160")
        self.assertEqual(self._reserved(), {})

    def test_a_line_naming_its_own_source_overrides_the_request(self):
        # 387 live SAP documents ship from more than one warehouse, so the
        # line's own source has to win where it is set.
        request = self._request(from_warehouse="BH-BT")
        self._line(request, requested="160", from_warehouse=SOURCE)
        self.assertEqual(self._reserved(), {ITEM: Decimal("160")})
        self.assertEqual(self._reserved(warehouse="BH-BT"), {})

    def test_another_company_is_not_counted(self):
        self._line(self._request(company=self.other_company), requested="160")
        self.assertEqual(self._reserved(), {})

    def test_item_codes_scope_the_query(self):
        self._line(self._request(), item_code=ITEM, requested="160")
        self._line(self._request(), item_code="FG0000142", requested="40")
        self.assertEqual(
            self._reserved(item_codes=[ITEM]), {ITEM: Decimal("160")},
        )
        self.assertEqual(self._reserved(item_codes=[]), {})

    def test_blank_warehouse_holds_nothing(self):
        self._line(self._request(), requested="160")
        self.assertEqual(self._reserved(warehouse="  "), {})
