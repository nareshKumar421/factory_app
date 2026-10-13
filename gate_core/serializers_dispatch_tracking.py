"""Serializers for post-dispatch truck tracking."""
from decimal import Decimal

from rest_framework import serializers

from gate_core.models import (
    TruckDispatchPartialDeliveryItem,
    TruckDispatchPartialDeliveryLine,
    TruckDispatchSapReceipt,
    TruckDispatchStatus,
    TruckDispatchUpdate,
)
from gate_core.services.dispatch_tracking_status import (
    delivered_on,
    delivery_progress,
    ship_to_groups,
)

# Statuses that carry a hand-over date the operator can back-date.
DELIVERY_STATUSES = (
    TruckDispatchStatus.DELIVERED,
    TruckDispatchStatus.PARTIALLY_DELIVERED,
)


class TruckDispatchBillItemSerializer(serializers.Serializer):
    """One item line on a bill, as offered to the partial-delivery form.

    ``quantity`` (in ``uom``) is the reference the operator splits — boxes are
    not populated on dispatched bills.
    """

    id = serializers.IntegerField()
    line_num = serializers.IntegerField()
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    uom = serializers.CharField()


class TruckDispatchBillSerializer(serializers.Serializer):
    """A bill on the truck, with its items, for the delivery form.

    Carries the bill's ship-to so the form can offer the truck's stops, and
    where its delivery stands (``context["progress"]``) so stops already made
    are shown as such and not offered again by default.
    """

    id = serializers.IntegerField()
    sap_doc_num = serializers.CharField()
    sap_doc_entry = serializers.IntegerField()
    customer_name = serializers.CharField()
    ship_to_code = serializers.CharField(default="")
    ship_to_address = serializers.CharField(default="")
    place_of_supply = serializers.CharField(default="")
    company = serializers.CharField(source="company.name", default="")
    total_quantity = serializers.DecimalField(max_digits=18, decimal_places=3, allow_null=True)
    sap_doc_total = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    # DELIVERED / PARTIALLY_DELIVERED once a delivery update covered this bill,
    # else null; and the date it was delivered on.
    delivery_status = serializers.SerializerMethodField()
    delivered_date = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()

    def _progress(self):
        return self.context.get("progress")

    def get_delivery_status(self, document):
        progress = self._progress()
        return progress.bill_status(document.id) if progress else None

    def get_delivered_date(self, document):
        progress = self._progress()
        return progress.bill_delivered_on(document.id) if progress else None

    def get_items(self, document):
        items = [item for item in document.items.all() if item.is_active]
        return TruckDispatchBillItemSerializer(items, many=True).data


class TruckDispatchPartialItemSerializer(serializers.ModelSerializer):
    """A per-item delivered/returned split within a bill."""

    item_code = serializers.CharField(source="item.item_code", read_only=True, default="")
    item_name = serializers.CharField(source="item.item_name", read_only=True, default="")
    uom = serializers.CharField(source="item.uom", read_only=True, default="")
    quantity = serializers.DecimalField(
        source="item.quantity", max_digits=18, decimal_places=3, read_only=True
    )

    class Meta:
        model = TruckDispatchPartialDeliveryItem
        fields = [
            "id",
            "item",
            "item_code",
            "item_name",
            "uom",
            "quantity",
            "qty_delivered",
            "qty_returned",
            "remarks",
        ]


class TruckDispatchPartialLineSerializer(serializers.ModelSerializer):
    """A per-bill delivered/returned split, with its item breakdown."""

    sap_doc_num = serializers.CharField(source="document.sap_doc_num", read_only=True, default="")
    customer_name = serializers.CharField(
        source="document.customer_name", read_only=True, default=""
    )
    total_quantity = serializers.DecimalField(
        source="document.total_quantity",
        max_digits=18,
        decimal_places=3,
        read_only=True,
        allow_null=True,
    )
    items = TruckDispatchPartialItemSerializer(many=True, read_only=True)

    class Meta:
        model = TruckDispatchPartialDeliveryLine
        fields = [
            "id",
            "document",
            "sap_doc_num",
            "customer_name",
            "total_quantity",
            "qty_delivered",
            "qty_returned",
            "remarks",
            "items",
        ]


class TruckDispatchSapReceiptSerializer(serializers.ModelSerializer):
    """Where one bill's delivery stands with SAP."""

    sap_doc_num = serializers.CharField(source="document.sap_doc_num", read_only=True, default="")
    customer_name = serializers.CharField(
        source="document.customer_name", read_only=True, default=""
    )
    company = serializers.CharField(source="document.company.name", read_only=True, default="")
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = TruckDispatchSapReceipt
        fields = [
            "id",
            "document",
            "sap_doc_num",
            "customer_name",
            "company",
            "status",
            "status_display",
            "received_date",
            "message",
            "posted_at",
        ]


class TruckDispatchUpdateSerializer(serializers.ModelSerializer):
    """One status event in a truck's post-dispatch timeline."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")
    partial_lines = TruckDispatchPartialLineSerializer(many=True, read_only=True)
    # A delivery, bill by bill, as SAP has it. Empty for every other status.
    sap_receipts = serializers.SerializerMethodField()
    # The bills a delivery at one stop covered, and that stop as the customer
    # locations name it. Both empty for an update that speaks for the truck.
    documents = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    locations = serializers.SerializerMethodField()

    def get_sap_receipts(self, update):
        receipts = [receipt for receipt in update.sap_receipts.all() if receipt.is_active]
        return TruckDispatchSapReceiptSerializer(receipts, many=True).data

    def get_locations(self, update):
        return [
            {key: value for key, value in group.items() if key != "bill_ids"}
            for group in ship_to_groups(update.documents.all())
        ]

    class Meta:
        model = TruckDispatchUpdate
        fields = [
            "id",
            "status",
            "status_display",
            "occurred_at",
            "expected_reach_date",
            "delivered_date",
            "location",
            "remarks",
            "proof",
            "return_note",
            "documents",
            "locations",
            "partial_lines",
            "sap_receipts",
            "created_by_name",
            "created_at",
        ]


class TruckDispatchPartialItemInputSerializer(serializers.Serializer):
    """One item's delivered/returned split, as submitted by the operator."""

    item = serializers.IntegerField()
    qty_delivered = serializers.DecimalField(
        max_digits=18, decimal_places=3, min_value=0, required=False, default=Decimal("0")
    )
    qty_returned = serializers.DecimalField(
        max_digits=18, decimal_places=3, min_value=0, required=False, default=Decimal("0")
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("qty_delivered") and not attrs.get("qty_returned"):
            raise serializers.ValidationError(
                "Enter the quantity delivered and/or returned for this item."
            )
        return attrs


class TruckDispatchPartialLineInputSerializer(serializers.Serializer):
    """One bill's shortfall, as submitted by the operator: its short items."""

    document = serializers.IntegerField()
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    items = TruckDispatchPartialItemInputSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError(
                "Record at least one short item for this bill."
            )
        seen = set()
        for entry in items:
            if entry["item"] in seen:
                raise serializers.ValidationError(
                    f"Item {entry['item']} is listed more than once on this bill."
                )
            seen.add(entry["item"])
        return items


class TruckDispatchUpdateCreateSerializer(serializers.Serializer):
    """Add a status event to a dispatched truck."""

    status = serializers.ChoiceField(choices=TruckDispatchStatus.choices)
    occurred_at = serializers.DateTimeField(required=False)
    # Expected reach date — meaningful on an In-Transit update.
    expected_reach_date = serializers.DateField(required=False, allow_null=True)
    # Hand-over date — meaningful on a Delivered / Partially Delivered update.
    delivered_date = serializers.DateField(required=False, allow_null=True)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    remarks = serializers.CharField(required=False, allow_blank=True)
    proof = serializers.FileField(required=False, allow_null=True)
    # Signed return note for the stock coming back on a partial delivery. Always
    # optional: the driver often brings it in a day later, so it can be attached
    # afterwards via the return-note endpoint.
    return_note = serializers.FileField(required=False, allow_null=True)
    partial_lines = TruckDispatchPartialLineInputSerializer(many=True, required=False)
    # The bills this delivery covers -- one stop of a multi-drop truck. Left out
    # (or empty), the update speaks for the whole truck.
    documents = serializers.ListField(child=serializers.IntegerField(), required=False)

    def validate(self, attrs):
        status = attrs.get("status")
        lines = attrs.get("partial_lines") or []
        documents = attrs.get("documents") or []

        if documents and status not in DELIVERY_STATUSES:
            raise serializers.ValidationError(
                {"documents": "Bills apply only to a Delivered or Partially Delivered update."}
            )
        if len(set(documents)) != len(documents):
            raise serializers.ValidationError(
                {"documents": "A bill is listed more than once."}
            )
        covered = set(documents)
        for line in lines:
            if covered and line["document"] not in covered:
                raise serializers.ValidationError({
                    "partial_lines": (
                        f"Bill {line['document']} is not among the bills this update "
                        "covers."
                    )
                })

        if status == TruckDispatchStatus.PARTIALLY_DELIVERED and not lines:
            raise serializers.ValidationError(
                {"partial_lines": "Record which bills were short and by how much."}
            )
        if lines and status != TruckDispatchStatus.PARTIALLY_DELIVERED:
            raise serializers.ValidationError(
                {"partial_lines": "Per-bill quantities apply only to a Partially Delivered update."}
            )
        if attrs.get("return_note") and status != TruckDispatchStatus.PARTIALLY_DELIVERED:
            raise serializers.ValidationError(
                {"return_note": "A return note applies only to a Partially Delivered update."}
            )
        if attrs.get("delivered_date") and status not in DELIVERY_STATUSES:
            raise serializers.ValidationError(
                {"delivered_date": "A delivered date applies only to a Delivered or "
                                   "Partially Delivered update."}
            )

        seen = set()
        for line in lines:
            document_id = line["document"]
            if document_id in seen:
                raise serializers.ValidationError(
                    {"partial_lines": f"Bill {document_id} is listed more than once."}
                )
            seen.add(document_id)

        return attrs


class TruckDispatchReturnNoteSerializer(serializers.Serializer):
    """Attach the return note to an existing partial delivery.

    The signed note usually comes back with the driver a day or two after the
    delivery is logged, so it is never required up front.
    """

    return_note = serializers.FileField(required=True)


class TruckDispatchProofSerializer(serializers.Serializer):
    """Attach the proof to an existing update -- for a delivery, what SAP waits on."""

    proof = serializers.FileField(required=True)


class DispatchTrackingTruckSerializer(serializers.Serializer):
    """A dispatched-truck row on the tracking board: the trip + its current status.

    Built from a prefetched ``VehicleArrival`` (``gate_outs__company``,
    ``gate_outs__documents``, ``dispatch_updates``).
    """

    arrival = serializers.IntegerField(source="id")
    arrival_no = serializers.CharField()
    arrival_status = serializers.CharField(source="status")
    vehicle = serializers.IntegerField(source="vehicle_id")
    vehicle_number = serializers.SerializerMethodField()
    transporter_name = serializers.SerializerMethodField()
    driver_name = serializers.SerializerMethodField()
    driver_mobile = serializers.SerializerMethodField()
    gatepass_no = serializers.CharField()
    dispatched_at = serializers.SerializerMethodField()
    companies = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    customers = serializers.SerializerMethodField()
    customer_locations = serializers.SerializerMethodField()
    current_status = serializers.SerializerMethodField()
    current_status_display = serializers.SerializerMethodField()
    last_update_at = serializers.SerializerMethodField()
    update_count = serializers.SerializerMethodField()
    # Expected reach date + whether the trip is overdue (date exceeded, not reached).
    expected_reach_date = serializers.SerializerMethodField()
    is_late = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()

    def _dispatched_dockings(self, arrival):
        return [
            docking
            for docking in arrival.gate_outs.all()
            if docking.is_active and docking.status == "DISPATCHED"
        ]

    def _progress(self, arrival):
        """The truck's delivery progress, worked out once per truck per request --
        the status, the lateness flags and the locations all read it."""
        cache = self.__dict__.setdefault("_progress_cache", {})
        progress = cache.get(arrival.id)
        if progress is None:
            progress = cache[arrival.id] = delivery_progress(arrival)
        return progress

    def get_vehicle_number(self, arrival):
        return getattr(arrival.vehicle, "vehicle_number", "") if arrival.vehicle_id else ""

    def get_transporter_name(self, arrival):
        """The transporter carrying this trip.

        The arrival itself holds no transporter — it lives on the docking, so it
        is read from the dispatched dockings the same way companies and customers
        are. Each docking's ``transporter_name`` is the frozen snapshot taken for
        the printed gatepass, which is what the trip actually went out under;
        the FK is only a fallback for a docking saved without one.

        A truck is one trip under one transporter, so this is normally a single
        name — but if a truck ever carries dockings booked under different
        transporters, all of them are joined rather than silently showing one and
        hiding the rest. Sorted, because docking order is not guaranteed and an
        arbitrary order would make the same truck read differently run to run.
        """
        names = set()
        for docking in self._dispatched_dockings(arrival):
            name = (docking.transporter_name or "").strip()
            if not name and docking.transporter_id:
                name = (getattr(docking.transporter, "name", "") or "").strip()
            if name:
                names.add(name)
        return ", ".join(sorted(names))

    def get_driver_name(self, arrival):
        return getattr(arrival.driver, "name", "") if arrival.driver_id else ""

    def get_driver_mobile(self, arrival):
        return getattr(arrival.driver, "mobile_no", "") if arrival.driver_id else ""

    def get_dispatched_at(self, arrival):
        if arrival.departed_at:
            return arrival.departed_at
        dispatched = [d.dispatched_at for d in self._dispatched_dockings(arrival) if d.dispatched_at]
        return max(dispatched) if dispatched else None

    def get_companies(self, arrival):
        seen = {}
        for docking in self._dispatched_dockings(arrival):
            company = docking.company
            if company and company.id not in seen:
                seen[company.id] = company.name or company.code
        return list(seen.values())

    def get_documents(self, arrival):
        numbers = []
        for docking in self._dispatched_dockings(arrival):
            docs = [d for d in docking.documents.all() if d.is_active]
            if docs:
                for document in docs:
                    if document.sap_doc_num and document.sap_doc_num not in numbers:
                        numbers.append(document.sap_doc_num)
            elif docking.sap_doc_num and docking.sap_doc_num not in numbers:
                # Legacy single-document docking: the bill is on the header.
                numbers.append(docking.sap_doc_num)
        return numbers

    def get_customers(self, arrival):
        names = []
        for docking in self._dispatched_dockings(arrival):
            docs = [d for d in docking.documents.all() if d.is_active]
            if docs:
                for document in docs:
                    if document.customer_name and document.customer_name not in names:
                        names.append(document.customer_name)
            elif docking.customer_name and docking.customer_name not in names:
                names.append(docking.customer_name)
        return names

    def get_customer_locations(self, arrival):
        """Where the truck's goods are going: each customer's ship-to address,
        and whether the truck has delivered there yet.

        Read off the bills the same way ``get_customers`` is — the header of a
        legacy single-document docking standing in for its bill — using the
        cells the Dispatch Sheet shows as Location (``ship_to_address``) and
        State (``place_of_supply``), so the two screens name the same place.

        One entry per distinct customer + ship-to, carrying the bills going
        there: a truck with four bills for one warehouse shows it once, while a
        customer taking deliveries at two addresses shows both. Each carries
        ``delivery_status`` (DELIVERED / PARTIALLY_DELIVERED / null) and
        ``delivered_date`` from the delivery updates that covered its bills.
        """
        progress = self._progress(arrival)
        sources = []
        for docking in self._dispatched_dockings(arrival):
            sources.extend([d for d in docking.documents.all() if d.is_active] or [docking])

        locations = []
        for group in ship_to_groups(sources):
            bill_ids = group.pop("bill_ids")
            if bill_ids:
                status, delivered_date = progress.group_status(bill_ids)
            elif progress.whole_truck is not None:
                # A legacy docking has no bill rows to scope a delivery to, so
                # only a whole-truck delivery can have reached it.
                status = progress.whole_truck.status
                delivered_date = delivered_on(progress.whole_truck)
            else:
                status, delivered_date = None, None
            group["delivery_status"] = status
            group["delivered_date"] = delivered_date
            locations.append(group)
        return locations

    def _latest_update(self, arrival):
        return self._progress(arrival).latest

    def get_current_status(self, arrival):
        return self._progress(arrival).current_status

    def get_current_status_display(self, arrival):
        return self._progress(arrival).current_status_display

    def get_last_update_at(self, arrival):
        latest = self._latest_update(arrival)
        return latest.occurred_at if latest else self.get_dispatched_at(arrival)

    def get_update_count(self, arrival):
        return len(arrival.dispatch_updates.all())

    def _eta(self, arrival):
        # The reach-by date from the most recent update that carries one (the
        # In-Transit update). Updates are ordered newest-first.
        for update in arrival.dispatch_updates.all():
            if update.expected_reach_date:
                return update.expected_reach_date
        return None

    def get_expected_reach_date(self, arrival):
        return self._eta(arrival)

    def get_is_late(self, arrival):
        # Late / date-exceeded: the reach-by date has passed and the truck is still
        # on the road (In Transit / Delayed) — i.e. it hasn't reached yet.
        eta = self._eta(arrival)
        if not eta:
            return False
        if self.get_current_status(arrival) not in (
            TruckDispatchStatus.IN_TRANSIT, TruckDispatchStatus.DELAYED,
        ):
            return False
        from django.utils import timezone
        return eta < timezone.localdate()

    def get_days_overdue(self, arrival):
        if not self.get_is_late(arrival):
            return 0
        from django.utils import timezone
        return (timezone.localdate() - self._eta(arrival)).days
