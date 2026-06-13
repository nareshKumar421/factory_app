from rest_framework import serializers
from .models import Transporter,Vehicle, VehicleType
from driver_management.models import VehicleEntry
from driver_management.serializers import DriverSerializer
from company.serializers import CompanySerializer
from quality_control.enums import InspectionStatus

class VehicleTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleType
        fields = [
            "id",
            "name",
        ]
        read_only_fields = ("id",)

class TransporterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transporter
        fields = [
            "id",
            "name",
            "contact_person",
            "mobile_no",
            "gstin",
            "created_at",
        ]
        read_only_fields = ("id", "created_at")

class TransporterNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transporter
        fields = [
            "id",
            "name",
        ]
        read_only_fields = ("id",)

class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = [
            "id",
            "vehicle_number",
            "vehicle_type",
            "transporter",
            "capacity_ton",
            "created_at",
        ]
        read_only_fields = ("id", "created_at")

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["transporter"] = TransporterSerializer(instance.transporter).data
        representation["vehicle_type"] = VehicleTypeSerializer(instance.vehicle_type).data
        return representation
    
class VehicleNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = [
            "id",
            "vehicle_number",
        ]
        read_only_fields = ("id",)


class VehicleEntrySerializer(serializers.ModelSerializer):
    def _get_qc_final_status(self, po_receipts):
        total_items = 0
        accepted_count = 0
        rejected_count = 0
        hold_count = 0
        pending_count = 0

        for po in po_receipts:
            for item in po.items.all():
                total_items += 1
                arrival_slip = getattr(item, "arrival_slip", None)
                inspection = getattr(arrival_slip, "inspection", None) if arrival_slip else None

                if not inspection:
                    pending_count += 1
                    continue

                if inspection.final_status == InspectionStatus.ACCEPTED:
                    accepted_count += 1
                elif inspection.final_status == InspectionStatus.REJECTED:
                    rejected_count += 1
                elif inspection.final_status == InspectionStatus.HOLD:
                    hold_count += 1
                else:
                    pending_count += 1

        if total_items == 0:
            return None

        if rejected_count:
            code = InspectionStatus.REJECTED
            display = "QC Rejected"
        elif accepted_count == total_items:
            code = InspectionStatus.ACCEPTED
            display = "QC Approved"
        elif hold_count:
            code = InspectionStatus.HOLD
            display = "QC On Hold"
        elif accepted_count:
            code = InspectionStatus.ACCEPTED
            display = f"QC Approved {accepted_count}/{total_items}"
        else:
            return None

        return {
            "code": code,
            "display": display,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "hold_count": hold_count,
            "pending_count": pending_count,
            "total_count": total_items,
        }

    class Meta:
        model = VehicleEntry
        fields = [
            "id",
            "entry_no",
            "company",
            "vehicle",
            "driver",
            "status",
            "entry_time",
            "entry_type",
            "remarks",
            "created_at",
            "updated_at",
        ]
        read_only_fields = (
            "id",
            "status",
            "entry_time",
            "created_at",
            "updated_at",
        )

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        po_receipts = list(instance.po_receipts.all())
        representation["vehicle"] = VehicleSerializer(instance.vehicle).data
        representation["driver"] = DriverSerializer(instance.driver, context={"request": self.context.get("request")}).data
        representation["company"] = CompanySerializer(instance.company).data
        representation["suppliers"] = [
            {"supplier_code": po.supplier_code, "supplier_name": po.supplier_name}
            for po in po_receipts
        ]
        representation["qc_final_status"] = (
            self._get_qc_final_status(po_receipts)
            if instance.entry_type == "RAW_MATERIAL"
            else None
        )
        return representation
    

