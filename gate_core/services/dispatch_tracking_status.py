"""The truck's current post-dispatch status, read off its timeline.

A truck delivered in one go is simple: its current status is its latest update.
A multi-drop truck -- one bilty, goods for Agra, Hisar and Bikaner -- is
delivered stop by stop, each stop logged as its own Delivered update covering
only that stop's bills (``TruckDispatchUpdate.documents``). The truck is then
Delivered only once every bill on it has been; until then it is Partially
Delivered, because part of the load is still on the road, even when nothing was
short at the stops already made.

A truck-level event (in transit, reached, delayed, returned, closed) still
speaks for the whole truck when it is the latest. An unscoped delivery -- one
made in one go, and every delivery logged before stops were tracked -- covers
every bill.

Everything here reads prefetched rows only (``gate_outs__documents``,
``dispatch_updates__documents``): the board serializes a page of trucks at a
time, and a query per truck would be an N+1.
"""
from dataclasses import dataclass, field

from django.utils import timezone

from gate_core.models import (
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
    TruckDispatchStatus,
)

DELIVERY_STATUSES = (
    TruckDispatchStatus.DELIVERED,
    TruckDispatchStatus.PARTIALLY_DELIVERED,
)


def truck_bills(arrival):
    """The live bills on the truck's dispatched dockings, every company's.

    A delivery is a fact about the truck, so its progress is measured over every
    bill it carries, not only those the caller's companies may see.
    """
    bills = []
    for docking in arrival.gate_outs.all():
        if not docking.is_active or docking.status != SalesDispatchGateOutStatus.DISPATCHED:
            continue
        bills.extend(document for document in docking.documents.all() if document.is_active)
    return bills


def scope_of(update):
    """The bill ids a delivery update covers; empty when it speaks for the truck."""
    return {document.id for document in update.documents.all()}


def delivered_on(update):
    """The business date an update delivered on: the hand-over date the operator
    gave, else the day the event happened."""
    return update.delivered_date or timezone.localdate(update.occurred_at)


def ship_to_groups(sources):
    """Where the goods on ``sources`` are going: one entry per distinct
    customer + ship-to, carrying the bills bound there.

    ``sources`` are bills, or a legacy single-document docking standing in for
    its bill. Read from the cells the Dispatch Sheet shows as Location
    (``ship_to_address``) and State (``place_of_supply``), so the two screens
    name the same place. ``bill_ids`` is for the caller's own bookkeeping and is
    not part of the API shape.
    """
    groups = {}
    for source in sources:
        if not (source.customer_name or source.ship_to_code or source.ship_to_address):
            continue
        key = (source.customer_name, source.ship_to_code, source.ship_to_address)
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = {
                "customer_name": source.customer_name,
                "ship_to_code": source.ship_to_code,
                "ship_to_address": source.ship_to_address,
                "place_of_supply": source.place_of_supply,
                "documents": [],
                "bill_ids": [],
            }
        if source.sap_doc_num and source.sap_doc_num not in entry["documents"]:
            entry["documents"].append(source.sap_doc_num)
        if isinstance(source, SalesDispatchGateOutDocument):
            entry["bill_ids"].append(source.id)
    return list(groups.values())


@dataclass
class DeliveryProgress:
    """How far along a truck's delivery is, bill by bill."""

    #: Every live bill on the truck.
    bill_ids: set = field(default_factory=set)
    #: Bill id -> the newest delivery update that covered it.
    covered: dict = field(default_factory=dict)
    #: The newest live update, whatever its status.
    latest: object = None
    #: The newest delivery update that spoke for the whole truck, if any.
    whole_truck: object = None

    def bill_status(self, bill_id):
        update = self.covered.get(bill_id)
        return update.status if update else None

    def bill_delivered_on(self, bill_id):
        update = self.covered.get(bill_id)
        return delivered_on(update) if update else None

    def group_status(self, bill_ids):
        """``(status, delivered_date)`` for bills delivered together -- a stop.

        DELIVERED once every bill is delivered in full; PARTIALLY_DELIVERED while
        only some are delivered, or any was short; ``(None, None)`` before any
        delivery reached it. The date is that of the latest delivery among them.
        """
        bill_ids = list(bill_ids)
        statuses = [self.bill_status(bill_id) for bill_id in bill_ids]
        if not any(statuses):
            return None, None
        dates = [self.bill_delivered_on(b) for b in bill_ids if b in self.covered]
        date = max(dates) if dates else None
        if all(status == TruckDispatchStatus.DELIVERED for status in statuses):
            return TruckDispatchStatus.DELIVERED.value, date
        return TruckDispatchStatus.PARTIALLY_DELIVERED.value, date

    @property
    def current_status(self):
        """The truck's current status, as a status value.

        The latest update -- unless that was a delivery at one stop, in which
        case the truck is Delivered only once every bill is, and Partially
        Delivered until then.
        """
        if self.latest is None:
            return "DISPATCHED"
        if self.latest.status in DELIVERY_STATUSES and scope_of(self.latest):
            status, _date = self.group_status(self.bill_ids)
            if status == TruckDispatchStatus.DELIVERED:
                return TruckDispatchStatus.DELIVERED.value
            return TruckDispatchStatus.PARTIALLY_DELIVERED.value
        return self.latest.status

    @property
    def current_status_display(self):
        status = self.current_status
        if status == "DISPATCHED":
            return "Dispatched"
        return TruckDispatchStatus(status).label


def delivery_progress(arrival):
    """Walk the truck's timeline, newest first, and note which bills each
    delivery covered -- a stop's bills for a scoped delivery, all of them for an
    unscoped one. The newest delivery to reach a bill is the one that counts."""
    bill_ids = {bill.id for bill in truck_bills(arrival)}
    updates = [update for update in arrival.dispatch_updates.all() if update.is_active]
    updates.sort(key=lambda update: (update.occurred_at, update.id), reverse=True)

    covered = {}
    whole_truck = None
    for update in updates:
        if update.status not in DELIVERY_STATUSES:
            continue
        scope = scope_of(update)
        if not scope:
            scope = bill_ids
            if whole_truck is None:
                whole_truck = update
        for bill_id in scope:
            covered.setdefault(bill_id, update)

    return DeliveryProgress(
        bill_ids=bill_ids,
        covered=covered,
        latest=updates[0] if updates else None,
        whole_truck=whole_truck,
    )
