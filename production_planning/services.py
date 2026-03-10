import logging
from typing import List, Dict, Any, Optional
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from sap_client.context import CompanyContext
from sap_client.client import SAPClient
from sap_client.hana.warehouse_reader import HanaWarehouseReader
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .models import (
    ProductionPlan, PlanMaterialRequirement,
    WeeklyPlan, DailyProductionEntry,
    PlanStatus, SAPSyncStatus,
)

logger = logging.getLogger(__name__)


class ProductionPlanningService:

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._context = None

    @property
    def context(self):
        if self._context is None:
            self._context = CompanyContext(self.company_code)
        return self._context

    # ------------------------------------------------------------------
    # Dropdown data from SAP HANA
    # ------------------------------------------------------------------

    def get_items_dropdown(self, item_type: str = None, search: str = None):
        """
        Fetch items from SAP HANA for dropdown.
        item_type: 'finished' | 'raw' | None (all)
        search: partial match on ItemCode or ItemName
        """
        from .sap.item_reader import HanaItemReader
        reader = HanaItemReader(self.context)

        if item_type == 'finished':
            return reader.get_finished_goods(search=search)
        elif item_type == 'raw':
            return reader.get_raw_materials(search=search)
        return reader.get_all_items(search=search)

    def get_uom_dropdown(self):
        """Fetch UoM list from SAP HANA."""
        from .sap.item_reader import HanaItemReader
        reader = HanaItemReader(self.context)
        return reader.get_uom_list()

    def get_warehouses_dropdown(self):
        """Fetch active warehouses from SAP HANA."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_active_warehouses()

    def get_bom_with_requirements(self, item_code: str, planned_qty: float) -> dict:
        """
        Fetch BOM components for item_code from SAP HANA (OITT/ITT1),
        scale quantities to planned_qty, and include stock/shortage data.
        """
        from .sap.bom_reader import HanaBOMReader
        reader = HanaBOMReader(self.context)
        return reader.get_bom(item_code=item_code, planned_qty=planned_qty)

    # ------------------------------------------------------------------
    # Plan CRUD (local DB)
    # ------------------------------------------------------------------

    @transaction.atomic
    def create_plan(self, data: dict, user) -> ProductionPlan:
        """Create a production plan locally (DRAFT status)."""
        from company.models import Company

        try:
            company = Company.objects.get(code=self.company_code)
        except Company.DoesNotExist:
            raise ValueError(f"Company '{self.company_code}' not found.")

        materials_data = data.pop('materials', [])

        plan = ProductionPlan.objects.create(
            company=company,
            item_code=data['item_code'],
            item_name=data['item_name'],
            uom=data.get('uom', ''),
            warehouse_code=data.get('warehouse_code', ''),
            planned_qty=data['planned_qty'],
            target_start_date=data['target_start_date'],
            due_date=data['due_date'],
            branch_id=data.get('branch_id'),
            remarks=data.get('remarks', ''),
            status=PlanStatus.DRAFT,
            sap_posting_status=SAPSyncStatus.NOT_POSTED,
            created_by=user,
        )

        for mat in materials_data:
            PlanMaterialRequirement.objects.create(
                production_plan=plan,
                component_code=mat['component_code'],
                component_name=mat['component_name'],
                required_qty=mat['required_qty'],
                uom=mat.get('uom', ''),
                warehouse_code=mat.get('warehouse_code', ''),
            )

        logger.info(f"Production plan created: ID={plan.id}, Item={plan.item_code}")
        return plan

    @transaction.atomic
    def update_plan(self, plan_id: int, data: dict) -> ProductionPlan:
        """Update a DRAFT plan. Only DRAFT plans can be edited."""
        plan = self._get_plan_or_raise(plan_id)

        if plan.status != PlanStatus.DRAFT:
            raise ValueError(
                f"Only DRAFT plans can be edited. Current status: '{plan.status}'."
            )

        for field in ['item_code', 'item_name', 'uom', 'warehouse_code',
                      'planned_qty', 'target_start_date', 'due_date',
                      'branch_id', 'remarks']:
            if field in data:
                setattr(plan, field, data[field])

        plan.save()
        return plan

    def delete_plan(self, plan_id: int):
        """Delete a DRAFT plan. Only DRAFT plans can be deleted."""
        plan = self._get_plan_or_raise(plan_id)

        if plan.status != PlanStatus.DRAFT:
            raise ValueError(
                f"Only DRAFT plans can be deleted. Current status: '{plan.status}'."
            )

        plan.delete()
        logger.info(f"Production plan {plan_id} deleted.")

    # ------------------------------------------------------------------
    # Material management
    # ------------------------------------------------------------------

    def add_material(self, plan_id: int, data: dict) -> PlanMaterialRequirement:
        """Add a BOM component to a DRAFT or OPEN plan."""
        plan = self._get_plan_or_raise(plan_id)

        if plan.status in (PlanStatus.COMPLETED, PlanStatus.CLOSED, PlanStatus.CANCELLED):
            raise ValueError(f"Cannot add materials to a plan with status '{plan.status}'.")

        return PlanMaterialRequirement.objects.create(
            production_plan=plan,
            component_code=data['component_code'],
            component_name=data['component_name'],
            required_qty=data['required_qty'],
            uom=data.get('uom', ''),
            warehouse_code=data.get('warehouse_code', ''),
        )

    def delete_material(self, plan_id: int, material_id: int):
        """Remove a BOM component. Only allowed on DRAFT plans."""
        plan = self._get_plan_or_raise(plan_id)

        if plan.status != PlanStatus.DRAFT:
            raise ValueError(f"Materials can only be removed from DRAFT plans.")

        try:
            material = PlanMaterialRequirement.objects.get(id=material_id, production_plan=plan)
        except PlanMaterialRequirement.DoesNotExist:
            raise ValueError(f"Material {material_id} not found for this plan.")

        material.delete()

    # ------------------------------------------------------------------
    # Post to SAP
    # ------------------------------------------------------------------

    def post_to_sap(self, plan_id: int, user) -> ProductionPlan:
        """
        Post a DRAFT plan to SAP B1 as a Production Order (OWOR).
        Updates plan with SAP DocEntry/DocNum on success.

        SAP endpoint: POST /b1s/v2/ProductionOrders
        """
        plan = self._get_plan_or_raise(plan_id)

        if plan.status not in (PlanStatus.DRAFT, PlanStatus.OPEN):
            raise ValueError(
                f"Only DRAFT or OPEN plans can be posted to SAP. Current status: '{plan.status}'."
            )

        if plan.sap_posting_status == SAPSyncStatus.POSTED:
            raise ValueError(
                f"Plan already posted to SAP. DocNum: {plan.sap_doc_num}"
            )

        # Build SAP payload
        # SAP B1 Service Layer field names for ProductionOrders entity.
        # NOTE: Do NOT send ProductionOrderLines — SAP auto-creates them from
        # the item's production BOM (OITT/ITT1) when ItemNo is provided.
        # DueDate must be within the company's allowed posting date range.
        payload = {
            "ItemNo": plan.item_code,
            "PlannedQuantity": float(plan.planned_qty),
            "DueDate": plan.due_date.strftime("%Y-%m-%d"),
            "ProductionOrderStatus": "boposPlanned",
        }

        if plan.target_start_date:
            payload["StartDate"] = plan.target_start_date.strftime("%Y-%m-%d")

        if plan.remarks:
            payload["Remarks"] = plan.remarks

        if plan.warehouse_code:
            payload["Warehouse"] = plan.warehouse_code

        logger.info(f"Posting production plan {plan_id} to SAP: {payload}")

        try:
            sap_client = SAPClient(company_code=self.company_code)
            result = sap_client.create_production_order(payload)

            plan.sap_doc_entry = result.get("DocEntry")
            plan.sap_doc_num = result.get("DocNum")
            plan.sap_status = result.get("Status", "R")
            plan.sap_posting_status = SAPSyncStatus.POSTED
            plan.sap_error_message = None
            plan.status = PlanStatus.OPEN
            plan.save(update_fields=[
                'sap_doc_entry', 'sap_doc_num', 'sap_status',
                'sap_posting_status', 'sap_error_message',
                'status', 'updated_at'
            ])

            logger.info(
                f"Production plan {plan_id} posted to SAP. "
                f"DocNum={plan.sap_doc_num}, DocEntry={plan.sap_doc_entry}"
            )
            return plan

        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            plan.sap_posting_status = SAPSyncStatus.FAILED
            plan.sap_error_message = str(e)
            plan.save(update_fields=['sap_posting_status', 'sap_error_message', 'updated_at'])
            logger.error(f"Failed to post production plan {plan_id} to SAP: {e}")
            raise

    # ------------------------------------------------------------------
    # Plan list / detail
    # ------------------------------------------------------------------

    def get_plans(self, status: str = None, month: str = None):
        qs = ProductionPlan.objects.filter(
            company__code=self.company_code
        ).select_related('created_by', 'closed_by')

        if status:
            qs = qs.filter(status=status)

        if month:
            try:
                year, mon = map(int, month.split('-'))
                qs = qs.filter(due_date__year=year, due_date__month=mon)
            except (ValueError, AttributeError):
                pass

        return qs

    def get_plan(self, plan_id: int) -> ProductionPlan:
        return self._get_plan_or_raise(plan_id)

    def _get_plan_or_raise(self, plan_id: int) -> ProductionPlan:
        try:
            return ProductionPlan.objects.prefetch_related(
                'materials', 'weekly_plans', 'weekly_plans__daily_entries'
            ).get(id=plan_id, company__code=self.company_code)
        except ProductionPlan.DoesNotExist:
            raise ValueError(f"Production plan {plan_id} not found.")

    # ------------------------------------------------------------------
    # Close plan
    # ------------------------------------------------------------------

    @transaction.atomic
    def close_plan(self, plan_id: int, user) -> ProductionPlan:
        plan = self._get_plan_or_raise(plan_id)

        if plan.status in (PlanStatus.CLOSED, PlanStatus.CANCELLED):
            raise ValueError(f"Cannot close a plan with status '{plan.status}'.")

        plan.recompute_completed_qty()
        plan.refresh_from_db()
        plan.status = PlanStatus.COMPLETED
        plan.closed_by = user
        plan.closed_at = timezone.now()
        plan.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])

        logger.info(
            f"Production plan {plan_id} closed by {user}. "
            f"Total produced: {plan.completed_qty}"
        )
        return plan

    # ------------------------------------------------------------------
    # Weekly plan
    # ------------------------------------------------------------------

    def create_weekly_plan(self, plan_id: int, data: dict, user) -> WeeklyPlan:
        plan = self._get_plan_or_raise(plan_id)

        if plan.status in (PlanStatus.DRAFT, PlanStatus.CLOSED, PlanStatus.CANCELLED):
            raise ValueError(
                f"Cannot add weekly plans to a plan with status '{plan.status}'. "
                f"Post the plan to SAP first."
            )

        weekly_plan = WeeklyPlan.objects.create(
            production_plan=plan,
            week_number=data['week_number'],
            week_label=data.get('week_label', ''),
            start_date=data['start_date'],
            end_date=data['end_date'],
            target_qty=data['target_qty'],
            created_by=user,
        )

        if plan.status == PlanStatus.OPEN:
            plan.status = PlanStatus.IN_PROGRESS
            plan.save(update_fields=['status', 'updated_at'])

        return weekly_plan

    def update_weekly_plan(self, plan_id: int, week_id: int, data: dict) -> WeeklyPlan:
        plan = self._get_plan_or_raise(plan_id)

        if plan.status in (PlanStatus.CLOSED, PlanStatus.CANCELLED):
            raise ValueError(f"Cannot edit weekly plans of a plan with status '{plan.status}'.")

        try:
            weekly_plan = WeeklyPlan.objects.get(id=week_id, production_plan=plan)
        except WeeklyPlan.DoesNotExist:
            raise ValueError(f"Weekly plan {week_id} not found.")

        if 'week_label' in data:
            weekly_plan.week_label = data['week_label']
        if 'target_qty' in data:
            weekly_plan.target_qty = data['target_qty']

        weekly_plan.save()
        return weekly_plan

    def delete_weekly_plan(self, plan_id: int, week_id: int):
        plan = self._get_plan_or_raise(plan_id)

        try:
            weekly_plan = WeeklyPlan.objects.get(id=week_id, production_plan=plan)
        except WeeklyPlan.DoesNotExist:
            raise ValueError(f"Weekly plan {week_id} not found.")

        if weekly_plan.daily_entries.exists():
            raise ValueError("Cannot delete a weekly plan that has daily production entries.")

        weekly_plan.delete()

    # ------------------------------------------------------------------
    # Daily entries
    # ------------------------------------------------------------------

    @transaction.atomic
    def add_daily_entry(self, week_id: int, data: dict, user) -> DailyProductionEntry:
        try:
            weekly_plan = WeeklyPlan.objects.select_related(
                'production_plan__company'
            ).get(id=week_id, production_plan__company__code=self.company_code)
        except WeeklyPlan.DoesNotExist:
            raise ValueError(f"Weekly plan {week_id} not found.")

        entry = DailyProductionEntry.objects.create(
            weekly_plan=weekly_plan,
            production_date=data['production_date'],
            produced_qty=data['produced_qty'],
            shift=data.get('shift'),
            remarks=data.get('remarks', ''),
            recorded_by=user,
        )

        weekly_plan.recompute_produced_qty()
        weekly_plan.production_plan.recompute_completed_qty()

        return entry

    @transaction.atomic
    def update_daily_entry(self, week_id: int, entry_id: int, data: dict) -> DailyProductionEntry:
        try:
            entry = DailyProductionEntry.objects.select_related(
                'weekly_plan__production_plan__company'
            ).get(
                id=entry_id,
                weekly_plan_id=week_id,
                weekly_plan__production_plan__company__code=self.company_code
            )
        except DailyProductionEntry.DoesNotExist:
            raise ValueError(f"Daily entry {entry_id} not found.")

        if 'produced_qty' in data:
            entry.produced_qty = data['produced_qty']
        if 'remarks' in data:
            entry.remarks = data['remarks']

        entry.save()
        entry.weekly_plan.recompute_produced_qty()
        entry.weekly_plan.production_plan.recompute_completed_qty()

        return entry

    def get_daily_entries(
        self, week_id: int = None, plan_id: int = None,
        date_from: str = None, date_to: str = None
    ):
        qs = DailyProductionEntry.objects.select_related(
            'weekly_plan__production_plan', 'recorded_by'
        ).filter(weekly_plan__production_plan__company__code=self.company_code)

        if week_id:
            qs = qs.filter(weekly_plan_id=week_id)
        if plan_id:
            qs = qs.filter(weekly_plan__production_plan_id=plan_id)
        if date_from:
            qs = qs.filter(production_date__gte=date_from)
        if date_to:
            qs = qs.filter(production_date__lte=date_to)

        return qs.order_by('-production_date')

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def get_summary(self, month: str = None) -> Dict[str, Any]:
        from django.db.models import Sum, Count

        qs = ProductionPlan.objects.filter(company__code=self.company_code)

        if month:
            try:
                year, mon = map(int, month.split('-'))
                qs = qs.filter(due_date__year=year, due_date__month=mon)
            except (ValueError, AttributeError):
                pass

        stats = qs.aggregate(
            total_plans=Count('id'),
            total_planned=Sum('planned_qty'),
            total_produced=Sum('completed_qty'),
        )

        status_breakdown = {s: qs.filter(status=s).count() for s in PlanStatus.values}
        sap_breakdown = {s: qs.filter(sap_posting_status=s).count() for s in SAPSyncStatus.values}

        total_planned = float(stats['total_planned'] or 0)
        total_produced = float(stats['total_produced'] or 0)
        overall_progress = (
            round(total_produced / total_planned * 100, 1) if total_planned else 0.0
        )

        return {
            'total_plans': stats['total_plans'] or 0,
            'total_planned_qty': total_planned,
            'total_produced_qty': total_produced,
            'overall_progress_percent': overall_progress,
            'status_breakdown': status_breakdown,
            'sap_posting_breakdown': sap_breakdown,
        }
