"""Serializers for post-dispatch truck tracking."""
from rest_framework import serializers

from gate_core.models import TruckDispatchStatus, TruckDispatchUpdate


class TruckDispatchUpdateSerializer(serializers.ModelSerializer):
    """One status event in a truck's post-dispatch timeline."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")

    class Meta:
        model = TruckDispatchUpdate
        fields = [
            "id",
            "status",
            "status_display",
            "occurred_at",
            "expected_reach_date",
            "location",
            "remarks",
            "proof",
            "created_by_name",
            "created_at",
        ]


class TruckDispatchUpdateCreateSerializer(serializers.Serializer):
    """Add a status event to a dispatched truck."""

    status = serializers.ChoiceField(choices=TruckDispatchStatus.choices)
    occurred_at = serializers.DateTimeField(required=False)
    # Expected reach date — meaningful on an In-Transit update.
    expected_reach_date = serializers.DateField(required=False, allow_null=True)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    remarks = serializers.CharField(required=False, allow_blank=True)
    proof = serializers.FileField(required=False, allow_null=True)


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
    driver_name = serializers.SerializerMethodField()
    driver_mobile = serializers.SerializerMethodField()
    gatepass_no = serializers.CharField()
    dispatched_at = serializers.SerializerMethodField()
    companies = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    customers = serializers.SerializerMethodField()
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

    def get_vehicle_number(self, arrival):
        return getattr(arrival.vehicle, "vehicle_number", "") if arrival.vehicle_id else ""

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

    def _latest_update(self, arrival):
        # dispatch_updates is prefetched and ordered -occurred_at, so [0] is newest.
        updates = list(arrival.dispatch_updates.all())
        return updates[0] if updates else None

    def get_current_status(self, arrival):
        latest = self._latest_update(arrival)
        return latest.status if latest else "DISPATCHED"

    def get_current_status_display(self, arrival):
        latest = self._latest_update(arrival)
        return latest.get_status_display() if latest else "Dispatched"

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
