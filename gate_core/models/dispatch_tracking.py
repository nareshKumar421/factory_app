"""Post-dispatch truck tracking: a log of status events on a dispatched truck.

Once a physical truck leaves the gate (its ``VehicleArrival`` is DEPARTED and its
dockings DISPATCHED), operators track what happens next -- in transit, reached,
delivered, returned, etc. Each event is one ``TruckDispatchUpdate`` on the truck's
arrival, so a multi-company truck has a single shared timeline (the trip is the
truck, not the per-company docking). The truck's *current* post-dispatch status is
the latest update.
"""
from django.db import models

from .base import BaseModel


class TruckDispatchStatus(models.TextChoices):
    """The post-dispatch lifecycle an operator logs against a dispatched truck.

    The truck leaves the gate as DISPATCHED (set by the dispatch flow); these are
    the stages that follow, added as the trip progresses.
    """

    IN_TRANSIT = "IN_TRANSIT", "In Transit"
    REACHED_DESTINATION = "REACHED_DESTINATION", "Reached Destination"
    UNLOADING = "UNLOADING", "Unloading"
    DELIVERED = "DELIVERED", "Delivered"
    PARTIALLY_DELIVERED = "PARTIALLY_DELIVERED", "Partially Delivered"
    RETURNED = "RETURNED", "Returned"
    DELAYED = "DELAYED", "Delayed"
    CLOSED = "CLOSED", "Closed"


# Terminal stages -- the trip is finished, no further updates expected.
TERMINAL_DISPATCH_STATUSES = (
    TruckDispatchStatus.DELIVERED,
    TruckDispatchStatus.RETURNED,
    TruckDispatchStatus.CLOSED,
)


class TruckDispatchUpdate(BaseModel):
    """One post-dispatch status event on a dispatched truck (its ``VehicleArrival``).

    Append-only: the truck's current post-dispatch status is the newest entry; the
    full set is the trip's timeline.
    """

    arrival = models.ForeignKey(
        "gate_core.VehicleArrival",
        on_delete=models.CASCADE,
        related_name="dispatch_updates",
    )
    status = models.CharField(max_length=32, choices=TruckDispatchStatus.choices)
    # When the event actually happened (may differ from when it was logged).
    occurred_at = models.DateTimeField()
    # The date the truck is expected to REACH its destination — captured on an
    # In-Transit update. Once this date passes and the truck hasn't reached, the trip
    # is flagged late / date-exceeded on the tracking board.
    expected_reach_date = models.DateField(null=True, blank=True)
    # The business date the goods were handed over — captured on a Delivered or
    # Partially Delivered update, where it is often back-dated to the day the
    # driver actually unloaded. Kept separate from ``occurred_at``: occurred_at
    # orders the timeline (and so decides the truck's current status), and a
    # back-dated delivery must not resurrect an older status as "latest".
    delivered_date = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)
    remarks = models.TextField(blank=True)
    # Optional proof of delivery / return (photo or document).
    proof = models.FileField(upload_to="dispatch_tracking/proof/", null=True, blank=True)
    # Signed return note for the stock coming back on a partial delivery. Separate
    # from ``proof`` so a partial delivery can carry both the delivery proof and
    # the return note.
    return_note = models.FileField(
        upload_to="dispatch_tracking/return_note/", null=True, blank=True
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["arrival", "-occurred_at"])]
        permissions = [
            ("can_view_dispatch_tracking", "Can view dispatch tracking"),
            ("can_update_dispatch_tracking", "Can add dispatch tracking updates"),
        ]

    def __str__(self):
        return f"arrival {self.arrival_id} · {self.status} @ {self.occurred_at:%Y-%m-%d %H:%M}"


class TruckDispatchPartialDeliveryLine(BaseModel):
    """How much of one bill was actually delivered on a partial delivery.

    A truck carries several bills (``SalesDispatchGateOutDocument``); on a
    PARTIALLY_DELIVERED update the operator records, per bill, how many boxes the
    customer took and how many came back. Only the bills that were short are
    recorded — a bill with no line was delivered in full.
    """

    update = models.ForeignKey(
        TruckDispatchUpdate,
        on_delete=models.CASCADE,
        related_name="partial_lines",
    )
    document = models.ForeignKey(
        "gate_core.SalesDispatchGateOutDocument",
        on_delete=models.PROTECT,
        related_name="partial_delivery_lines",
    )
    # Boxes, matching SalesDispatchGateOutDocument.total_boxes — the unit the gate
    # and the transporter both count in.
    boxes_delivered = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    boxes_returned = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["update"]), models.Index(fields=["document"])]
        constraints = [
            models.UniqueConstraint(
                fields=["update", "document"],
                name="unique_partial_delivery_line_per_bill",
            )
        ]

    def __str__(self):
        return f"update {self.update_id} · bill {self.document_id} · {self.boxes_returned} returned"
