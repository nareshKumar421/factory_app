from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Sequence

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from sap_client.context import CompanyContext
from vehicle_management.models import Transporter, Vehicle

from .hana_reader import HanaDispatchBillReader
from .models import DispatchPlan, DispatchPlanStatus
from .serializers import DispatchPlanSerializer


class DispatchPlansService:
    def __init__(self, company_code: str):
        self.company_code = company_code
        self.company = Company.objects.get(code=company_code)
        self.context = CompanyContext(company_code)
        self.reader = HanaDispatchBillReader(self.context)

    def get_bills(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        rows = self.reader.list_bills(filters)
        doc_entries = [row["doc_entry"] for row in rows]
        plans = {
            plan.sap_invoice_doc_entry: plan
            for plan in DispatchPlan.objects.filter(
                company=self.company,
                sap_invoice_doc_entry__in=doc_entries,
                is_active=True,
            )
        }

        data = []
        for row in rows:
            plan = plans.get(row["doc_entry"])
            row["plan"] = (
                DispatchPlanSerializer(plan).data
                if plan
                else self._empty_plan(row["doc_entry"], row["doc_num"])
            )
            data.append(row)

        booking_status = filters.get("booking_status") or "all"
        if booking_status != "all":
            data = [
                row
                for row in data
                if row["plan"]["booking_status"] == booking_status
            ]

        if filters.get("exclude_jivo_mart_transfer"):
            data = [
                row
                for row in data
                if not self._is_jivo_oil_to_jivo_mart_transfer(row)
            ]

        search = (filters.get("search") or "").strip().lower()
        if search:
            data = [row for row in data if self._matches_search(row, search)]

        return {
            "data": data,
            "meta": self._build_meta(data),
        }

    def get_bill_by_number(self, invoice_number: str) -> Dict[str, Any] | None:
        bill = self.reader.get_bill_by_number(invoice_number.strip())
        if not bill:
            return None

        plan = DispatchPlan.objects.filter(
            company=self.company,
            sap_invoice_doc_entry=bill["doc_entry"],
            is_active=True,
        ).first()
        bill["plan"] = (
            DispatchPlanSerializer(plan).data
            if plan
            else self._empty_plan(bill["doc_entry"], bill["doc_num"])
        )
        return bill

    def get_schedule_enrichment(self, doc_entries: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        """One SAP query: per-invoice item summary, source warehouses, and totals.

        Keyed by SAP doc entry. Used to enrich the read-only warehouse dispatch
        schedule with what to issue and from where, without storing items locally.
        """
        doc_entries = [int(d) for d in dict.fromkeys(doc_entries or [])]
        if not doc_entries:
            return {}

        rows = self.reader.list_bills_by_doc_entries(doc_entries)
        enrichment: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            enrichment[row["doc_entry"]] = {
                "item_summary": row.get("item_summary", ""),
                "warehouses": row.get("warehouses", ""),
                "line_count": row.get("line_count", 0),
                "total_boxes": row.get("total_boxes", 0),
                "total_litres": row.get("total_litres", 0),
                "total_weight": row.get("total_weight", 0),
            }
        return enrichment

    def get_schedule_line_items(self, doc_entry: int) -> List[Dict[str, Any]]:
        """Full SAP line items for one scheduled invoice (loaded on demand)."""
        return self.reader.list_bill_lines(int(doc_entry))

    def update_plan(
        self,
        sap_invoice_doc_entry: int,
        data: Dict[str, Any],
        user,
    ) -> DispatchPlan:
        linked_doc_entries = data.pop("linked_invoice_doc_entries", None)
        if linked_doc_entries:
            return self.update_linked_plans(
                primary_sap_invoice_doc_entry=sap_invoice_doc_entry,
                linked_sap_invoice_doc_entries=linked_doc_entries,
                data=data,
                user=user,
            )

        return self._update_single_plan(
            sap_invoice_doc_entry=sap_invoice_doc_entry,
            data=data,
            user=user,
        )

    def update_linked_plans(
        self,
        primary_sap_invoice_doc_entry: int,
        linked_sap_invoice_doc_entries: List[int],
        data: Dict[str, Any],
        user,
    ) -> DispatchPlan:
        doc_entries = list(
            dict.fromkeys([primary_sap_invoice_doc_entry, *linked_sap_invoice_doc_entries])
        )
        if len(doc_entries) == 1:
            return self._update_single_plan(
                sap_invoice_doc_entry=primary_sap_invoice_doc_entry,
                data=data,
                user=user,
            )

        bills = self.reader.list_bills_by_doc_entries(doc_entries)
        bills_by_doc_entry = {bill["doc_entry"]: bill for bill in bills}
        missing = [doc_entry for doc_entry in doc_entries if doc_entry not in bills_by_doc_entry]
        if missing:
            raise ValueError(f"Selected dispatch invoice(s) were not found in SAP: {missing}")

        branch_ids = {
            bill["branch_id"] for bill in bills if bill.get("branch_id") is not None
        }
        if len(branch_ids) > 1:
            raise ValueError("Selected invoices must belong to the same SAP branch.")

        shared_data = self._shared_batch_link_data(data)
        allocations = self._allocate_batch_freight(
            bills=[bills_by_doc_entry[doc_entry] for doc_entry in doc_entries],
            amount=data.get("total_freight") or data.get("freight"),
        )

        updated_plans = []
        for index, doc_entry in enumerate(doc_entries):
            bill = bills_by_doc_entry[doc_entry]
            plan_data = {
                **shared_data,
                **self._invoice_defaults_from_bill(bill),
            }
            if allocations:
                plan_data["freight"] = allocations[doc_entry]
                plan_data["total_freight"] = allocations[doc_entry]

            bilty_attachment = plan_data.get("bilty_attachment")
            if bilty_attachment and hasattr(bilty_attachment, "seek"):
                bilty_attachment.seek(0)

            updated_plans.append(
                self._update_single_plan(
                    sap_invoice_doc_entry=doc_entry,
                    data=plan_data,
                    user=user,
                )
            )

        return next(
            plan
            for plan in updated_plans
            if plan.sap_invoice_doc_entry == primary_sap_invoice_doc_entry
        )

    def _update_single_plan(
        self,
        sap_invoice_doc_entry: int,
        data: Dict[str, Any],
        user,
    ) -> DispatchPlan:
        doc_num = data.pop("sap_invoice_doc_num", "")
        bilty_attachment = data.get("bilty_attachment")
        self._validate_links(data)
        self._apply_master_data(data)
        plan, created = DispatchPlan.objects.get_or_create(
            company=self.company,
            sap_invoice_doc_entry=sap_invoice_doc_entry,
            defaults={
                "sap_invoice_doc_num": doc_num,
                "created_by": user,
                "updated_by": user,
            },
        )

        if doc_num:
            plan.sap_invoice_doc_num = doc_num

        for field, value in data.items():
            setattr(plan, field, value)

        if created and not plan.booking_status:
            plan.booking_status = DispatchPlanStatus.PENDING

        if bilty_attachment:
            plan.bilty_attachment_name = getattr(
                bilty_attachment,
                "name",
                plan.bilty_attachment_name,
            )

        plan.updated_by = user
        plan.save()
        return plan

    @staticmethod
    def _shared_batch_link_data(data: Dict[str, Any]) -> Dict[str, Any]:
        invoice_specific_fields = {
            "sap_invoice_doc_num",
            "invoice_number",
            "eway_bill",
            "invoice_weight",
            "invoice_amount",
            "place_of_supply",
            "product_variety",
            "total_litres",
            "effective_month",
        }
        return {
            field: value
            for field, value in data.items()
            if field not in invoice_specific_fields
        }

    @classmethod
    def _invoice_defaults_from_bill(cls, bill: Dict[str, Any]) -> Dict[str, Any]:
        place_of_supply = bill.get("state") or bill.get("city") or ""
        return {
            "sap_invoice_doc_num": bill.get("doc_num") or "",
            "invoice_number": bill.get("doc_num") or "",
            "eway_bill": bill.get("sap_eway_bill") or "",
            "invoice_weight": bill.get("total_weight") or None,
            "invoice_amount": bill.get("doc_total") or None,
            "place_of_supply": place_of_supply,
            "product_variety": cls._infer_product_variety(bill.get("item_summary") or ""),
            "total_litres": bill.get("total_litres") or None,
            "effective_month": cls._month_start(bill.get("doc_date")),
            "budget_delivery_point": bill.get("city") or "",
        }

    @staticmethod
    def _infer_product_variety(item_summary: str) -> str:
        normalized = (item_summary or "").lower()
        if any(
            token in normalized
            for token in ("water", "mineral", "drink", "beverage", "juice")
        ):
            return "Beverage"
        return "Oil" if item_summary.strip() else ""

    @staticmethod
    def _month_start(value: Any):
        if not value:
            return None
        if hasattr(value, "date"):
            value = value.date()
        if hasattr(value, "replace") and not isinstance(value, str):
            return value.replace(day=1)
        try:
            parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            return parsed.replace(day=1)
        except ValueError:
            return None

    @staticmethod
    def _allocate_batch_freight(
        bills: List[Dict[str, Any]],
        amount: Any,
    ) -> Dict[int, Decimal]:
        if amount in (None, ""):
            return {}
        total_amount = Decimal(str(amount))
        if total_amount <= 0 or not bills:
            return {}

        weights = []
        for bill in bills:
            weight = Decimal(str(bill.get("total_litres") or 0))
            if weight <= 0:
                weight = Decimal(str(bill.get("total_weight") or 0))
            if weight <= 0:
                weight = Decimal(str(bill.get("doc_total") or 0))
            weights.append(weight if weight > 0 else Decimal("1"))

        total_weight = sum(weights, Decimal("0"))
        allocations: Dict[int, Decimal] = {}
        running_total = Decimal("0")
        for index, bill in enumerate(bills):
            doc_entry = int(bill["doc_entry"])
            if index == len(bills) - 1:
                allocation = total_amount - running_total
            else:
                allocation = (total_amount * weights[index] / total_weight).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
                running_total += allocation
            allocations[doc_entry] = allocation
        return allocations

    def _empty_plan(self, doc_entry: int, doc_num: str) -> Dict[str, Any]:
        return {
            "id": None,
            "sap_invoice_doc_entry": doc_entry,
            "sap_invoice_doc_num": doc_num,
            "invoice_number": "",
            "eway_bill": "",
            "invoice_weight": None,
            "invoice_amount": None,
            "place_of_supply": "",
            "product_variety": "",
            "total_litres": None,
            "effective_month": None,
            "budget_delivery_point": "",
            "service_location_code": None,
            "service_location_name": "",
            "sac_entry": None,
            "sac_code": "",
            "vehicle_id": None,
            "transporter_id": None,
            "driver_id": None,
            "linked_vehicle_entry_id": None,
            "booking_status": DispatchPlanStatus.PENDING,
            "dispatch_date": None,
            "priority": "",
            "transporter_name": "",
            "transporter_gstin": "",
            "contact_person": "",
            "mobile_no": "",
            "vehicle_no": "",
            "driver_name": "",
            "driver_mobile_no": "",
            "driver_license_no": "",
            "driver_id_proof_type": "",
            "driver_id_proof_number": "",
            "bilty_no": "",
            "bilty_date": None,
            "bilty_attachment": None,
            "bilty_attachment_name": "",
            "freight": None,
            "total_freight": None,
            "kanta_weight": None,
            "remarks": "",
            "created_at": None,
            "updated_at": None,
        }

    @staticmethod
    def _matches_search(row: Dict[str, Any], search: str) -> bool:
        plan = row.get("plan") or {}
        values = [
            row.get("doc_num"),
            row.get("card_code"),
            row.get("card_name"),
            row.get("ship_to_code"),
            row.get("ship_to_address"),
            row.get("state"),
            row.get("city"),
            row.get("bp_gstin"),
            row.get("sap_bilty_no"),
            row.get("sap_transporter_name"),
            row.get("sap_vehicle_no"),
            row.get("sap_transporter_invoice"),
            row.get("sap_lr_number"),
            row.get("sap_eway_bill"),
            row.get("gst_vehicle_no"),
            row.get("warehouses"),
            row.get("item_summary"),
            row.get("base_refs"),
            plan.get("transporter_name"),
            plan.get("transporter_gstin"),
            plan.get("contact_person"),
            plan.get("mobile_no"),
            plan.get("vehicle_no"),
            plan.get("driver_name"),
            plan.get("driver_mobile_no"),
            plan.get("driver_license_no"),
            plan.get("invoice_number"),
            plan.get("eway_bill"),
            plan.get("place_of_supply"),
            plan.get("bilty_no"),
            plan.get("remarks"),
        ]
        return any(search in str(value or "").lower() for value in values)

    def _is_jivo_oil_to_jivo_mart_transfer(self, row: Dict[str, Any]) -> bool:
        if (self.company_code or "").upper() != "JIVO_OIL":
            return False

        destination_values = [
            row.get("card_code"),
            row.get("card_name"),
            row.get("ship_to_code"),
            row.get("ship_to_address"),
            row.get("bp_gstin"),
        ]
        return any(self._looks_like_jivo_mart(value) for value in destination_values)

    @staticmethod
    def _looks_like_jivo_mart(value: Any) -> bool:
        normalized = str(value or "").upper().replace("_", " ")
        normalized = " ".join(normalized.split())
        return "JIVO MART" in normalized or "JIVOMART" in normalized

    def _validate_links(self, data: Dict[str, Any]) -> None:
        vehicle_id = data.get("vehicle_id")
        if vehicle_id and not Vehicle.objects.filter(pk=vehicle_id, is_active=True).exists():
            raise ValueError("Selected vehicle does not exist.")

        transporter_id = data.get("transporter_id")
        if transporter_id and not Transporter.objects.filter(
            pk=transporter_id,
            is_active=True,
        ).exists():
            raise ValueError("Selected transporter does not exist.")

        driver_id = data.get("driver_id")
        if driver_id and not Driver.objects.filter(pk=driver_id, is_active=True).exists():
            raise ValueError("Selected driver does not exist.")

        linked_vehicle_entry_id = data.get("linked_vehicle_entry_id")
        if linked_vehicle_entry_id and not VehicleEntry.objects.filter(
            pk=linked_vehicle_entry_id,
            company=self.company,
        ).exists():
            raise ValueError("Selected gate vehicle entry does not exist for this company.")

    @staticmethod
    def _set_if_not_explicit(
        data: Dict[str, Any],
        explicit_fields: set[str],
        field: str,
        value: Any,
    ) -> None:
        if field not in explicit_fields:
            data[field] = value

    @classmethod
    def _apply_master_data(cls, data: Dict[str, Any]) -> None:
        explicit_fields = set(data)

        linked_vehicle_entry = None
        linked_vehicle_entry_id = data.get("linked_vehicle_entry_id")
        if linked_vehicle_entry_id:
            linked_vehicle_entry = VehicleEntry.objects.select_related(
                "vehicle__transporter",
                "driver",
            ).get(pk=linked_vehicle_entry_id)
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "vehicle_id",
                linked_vehicle_entry.vehicle_id,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "driver_id",
                linked_vehicle_entry.driver_id,
            )
            if linked_vehicle_entry.vehicle.transporter_id:
                cls._set_if_not_explicit(
                    data,
                    explicit_fields,
                    "transporter_id",
                    linked_vehicle_entry.vehicle.transporter_id,
                )

        vehicle = None
        vehicle_id = data.get("vehicle_id")
        if vehicle_id:
            vehicle = Vehicle.objects.select_related("transporter").get(pk=vehicle_id)
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "vehicle_no",
                vehicle.vehicle_number,
            )
            if vehicle.transporter_id and "transporter_id" not in explicit_fields:
                data["transporter_id"] = vehicle.transporter_id
        elif "vehicle_id" in explicit_fields:
            cls._set_if_not_explicit(data, explicit_fields, "vehicle_no", "")

        transporter = None
        transporter_id = data.get("transporter_id")
        if transporter_id:
            transporter = Transporter.objects.get(pk=transporter_id)
        elif vehicle and vehicle.transporter_id:
            transporter = vehicle.transporter

        if transporter:
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "transporter_name",
                transporter.name,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "transporter_gstin",
                getattr(
                    transporter,
                    "gstin",
                    data.get("transporter_gstin", ""),
                ),
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "contact_person",
                transporter.contact_person,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "mobile_no",
                transporter.mobile_no,
            )
        elif "transporter_id" in explicit_fields:
            cls._set_if_not_explicit(data, explicit_fields, "transporter_name", "")
            cls._set_if_not_explicit(data, explicit_fields, "transporter_gstin", "")
            cls._set_if_not_explicit(data, explicit_fields, "contact_person", "")
            cls._set_if_not_explicit(data, explicit_fields, "mobile_no", "")

        driver_id = data.get("driver_id")
        if driver_id:
            driver = Driver.objects.get(pk=driver_id)
            cls._set_if_not_explicit(data, explicit_fields, "driver_name", driver.name)
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "driver_mobile_no",
                driver.mobile_no,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "driver_license_no",
                driver.license_no,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "driver_id_proof_type",
                driver.id_proof_type,
            )
            cls._set_if_not_explicit(
                data,
                explicit_fields,
                "driver_id_proof_number",
                driver.id_proof_number,
            )
        elif "driver_id" in explicit_fields:
            cls._set_if_not_explicit(data, explicit_fields, "driver_name", "")
            cls._set_if_not_explicit(data, explicit_fields, "driver_mobile_no", "")
            cls._set_if_not_explicit(data, explicit_fields, "driver_license_no", "")
            cls._set_if_not_explicit(data, explicit_fields, "driver_id_proof_type", "")
            cls._set_if_not_explicit(data, explicit_fields, "driver_id_proof_number", "")

    @staticmethod
    def _build_meta(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        statuses = [row["plan"]["booking_status"] for row in rows]
        return {
            "total_bills": len(rows),
            "pending_count": statuses.count(DispatchPlanStatus.PENDING),
            "booked_count": statuses.count(DispatchPlanStatus.BOOKED),
            "dispatched_count": statuses.count(DispatchPlanStatus.DISPATCHED),
            "cancelled_count": statuses.count(DispatchPlanStatus.CANCELLED),
            "total_doc_value": round(sum(row["doc_total"] for row in rows), 2),
            "total_litres": round(sum(row["total_litres"] for row in rows), 3),
            "total_boxes": round(sum(row["total_boxes"] for row in rows), 3),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
