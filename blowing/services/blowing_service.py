import logging

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from decimal import Decimal
from datetime import date, datetime

from ..models import (
    BlowingMachine, PreformSpec, BlowingRateConfig, BlowingRun, RunStatus,
    BottleBuyPrice, BlowingSegment, BlowingBreakdown, BlowingBreakdownCategory,
    WarehouseApprovalStatus, BlowingAuditLog, BlowingCostRate,
)
from .cost_calculator import recalculate_run_cost

MACHINE_EDIT_FIELDS = ['name', 'heads', 'sap_warehouse_code', 'depreciation_per_day', 'is_active']


def _audit_value(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v

# Fields the operator fills in at Complete (readings/production/manpower moved
# out of the create form into the run lifecycle).
COMPLETION_FIELDS = [
    'total_counter_production', 'rejection_pcs',
    'operator_count', 'contract_labour_count', 'own_labour_count',
    'machine_start_reading', 'machine_stop_reading', 'utility_units',
    'scrap_carton_value',
]

BUY_PRICE_FIELDS = [
    'supplier_name', 'effective_from', 'buy_price', 'freight_per_bottle',
    'duties_per_bottle', 'carrying_pct_annual', 'inventory_days',
    'qa_allowance_pct', 'risk_premium_per_bottle',
]

PREFORM_SPEC_OPTIONAL_FIELDS = [
    'preform_rate_per_bottle',
    'sap_item_code', 'sap_item_name', 'bottle_weight_g', 'bottles_per_kg',
    'mould_cost', 'mould_life_bottles',
    'std_make_cost_per_bottle', 'std_reject_pct', 'std_units_per_bottle',
]

logger = logging.getLogger(__name__)

# Inputs copyable straight from request data onto a run.
RUN_INPUT_FIELDS = [
    'preform_boxes_used', 'machine_start_reading', 'machine_stop_reading',
    'utility_units', 'total_counter_production', 'rejection_pcs',
    'operator_count', 'contract_labour_count', 'own_labour_count',
    'scrap_carton_value', 'remarks',
]

RATE_SNAPSHOT_FIELDS = [
    'operator_rate_per_day', 'labour_rate_per_day', 'electricity_rate_per_unit',
    'scrap_rate_per_bottle', 'packing_rate_per_bottle',
    'maintenance_per_day', 'factory_overhead_per_day', 'qa_cost_per_day',
]


class BlowingService:
    """Company-scoped orchestration for the blowing module. Raises ValueError for
    all business errors (views translate to HTTP 400)."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._company = None

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            try:
                self._company = Company.objects.get(code=self.company_code)
            except Company.DoesNotExist:
                raise ValueError(f"Company '{self.company_code}' not found.")
        return self._company

    # ==================================================================
    # Machines
    # ==================================================================
    def list_machines(self, is_active=None):
        qs = BlowingMachine.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_machine(self, data: dict, user=None) -> BlowingMachine:
        machine = BlowingMachine.objects.create(
            company=self.company,
            name=data['name'],
            heads=data.get('heads'),
            sap_warehouse_code=data.get('sap_warehouse_code', ''),
            depreciation_per_day=data.get('depreciation_per_day', 0),
            created_by=user,
        )
        self._log_create('machine', machine, ['name', 'heads', 'sap_warehouse_code', 'depreciation_per_day'], user)
        return machine

    def update_machine(self, machine_id: int, data: dict, user=None) -> BlowingMachine:
        machine = self._get_machine_or_raise(machine_id)
        changes = self._apply_with_diff(machine, data, MACHINE_EDIT_FIELDS)
        machine.updated_by = user
        machine.save()
        self._log_update('machine', machine.id, changes, user)
        return machine

    # ---- audit helpers ----
    def _apply_with_diff(self, obj, data: dict, fields) -> dict:
        changes = {}
        for f in fields:
            if f in data:
                old = getattr(obj, f)
                new = data[f]
                if str(old) != str(new):
                    changes[f] = {'old': _audit_value(old), 'new': _audit_value(new)}
                    setattr(obj, f, new)
        return changes

    def _log_create(self, entity_type, obj, fields, user):
        changes = {f: {'old': None, 'new': _audit_value(getattr(obj, f))} for f in fields}
        BlowingAuditLog.objects.create(
            company=self.company, entity_type=entity_type, entity_id=obj.id,
            action=BlowingAuditLog.Action.CREATE, changes=changes, user=user)

    def _log_update(self, entity_type, entity_id, changes, user):
        if not changes:
            return
        BlowingAuditLog.objects.create(
            company=self.company, entity_type=entity_type, entity_id=entity_id,
            action=BlowingAuditLog.Action.UPDATE, changes=changes, user=user)

    def list_audit_logs(self, entity_type, entity_id):
        return BlowingAuditLog.objects.filter(
            company=self.company, entity_type=entity_type, entity_id=entity_id)

    def delete_machine(self, machine_id: int):
        machine = self._get_machine_or_raise(machine_id)
        machine.is_active = False
        machine.save(update_fields=['is_active', 'updated_at'])

    def _get_machine_or_raise(self, machine_id) -> BlowingMachine:
        try:
            return BlowingMachine.objects.get(id=machine_id, company=self.company)
        except BlowingMachine.DoesNotExist:
            raise ValueError("Blowing machine not found.")

    # ==================================================================
    # Preform specs
    # ==================================================================
    def list_preform_specs(self, is_active=None):
        qs = PreformSpec.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_preform_spec(self, data: dict, user=None) -> PreformSpec:
        spec = PreformSpec.objects.create(
            company=self.company,
            make=data['make'],
            gram=data['gram'],
            preforms_per_box=data['preforms_per_box'],
            created_by=user,
            **{k: data[k] for k in PREFORM_SPEC_OPTIONAL_FIELDS if k in data}
        )
        self._log_create('preform_spec', spec, ['make', 'gram', 'preforms_per_box', *PREFORM_SPEC_OPTIONAL_FIELDS], user)
        return spec

    def update_preform_spec(self, spec_id: int, data: dict, user=None) -> PreformSpec:
        spec = self._get_spec_or_raise(spec_id)
        changes = self._apply_with_diff(
            spec, data, ['make', 'gram', 'preforms_per_box', *PREFORM_SPEC_OPTIONAL_FIELDS, 'is_active'])
        spec.updated_by = user
        spec.save()
        self._log_update('preform_spec', spec.id, changes, user)
        return spec

    def delete_preform_spec(self, spec_id: int):
        spec = self._get_spec_or_raise(spec_id)
        spec.is_active = False
        spec.save(update_fields=['is_active', 'updated_at'])

    def _get_spec_or_raise(self, spec_id) -> PreformSpec:
        try:
            return PreformSpec.objects.get(id=spec_id, company=self.company)
        except PreformSpec.DoesNotExist:
            raise ValueError("Preform spec not found.")

    # ==================================================================
    # Rate config
    # ==================================================================
    def list_rate_configs(self, is_active=None):
        qs = BlowingRateConfig.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_rate_config(self, data: dict, user=None) -> BlowingRateConfig:
        return BlowingRateConfig.objects.create(
            company=self.company, created_by=user, **{
                k: data[k] for k in [
                    'effective_from', *RATE_SNAPSHOT_FIELDS,
                ] if k in data
            }
        )

    def update_rate_config(self, config_id: int, data: dict, user=None) -> BlowingRateConfig:
        rc = self._get_rate_config_or_raise(config_id)
        for field in ['effective_from', *RATE_SNAPSHOT_FIELDS, 'is_active']:
            if field in data:
                setattr(rc, field, data[field])
        rc.updated_by = user
        rc.save()
        return rc

    def _get_rate_config_or_raise(self, config_id) -> BlowingRateConfig:
        try:
            return BlowingRateConfig.objects.get(id=config_id, company=self.company)
        except BlowingRateConfig.DoesNotExist:
            raise ValueError("Rate config not found.")

    def get_effective_rate_config(self, date) -> BlowingRateConfig:
        return (
            BlowingRateConfig.objects
            .filter(company=self.company, is_active=True, effective_from__lte=date)
            .order_by('-effective_from')
            .first()
        )

    # ==================================================================
    # Cost Master — catalog of per-category rates (company + per-machine)
    # ==================================================================
    def list_cost_rates(self, scope=None, machine_id=None):
        """scope='global' → company-wide defaults only; machine_id → that
        machine's overrides; neither → all active rates."""
        qs = (
            BlowingCostRate.objects
            .filter(company=self.company, is_active=True)
            .select_related('machine')
        )
        if scope == 'global':
            qs = qs.filter(machine__isnull=True)
        if machine_id:
            qs = qs.filter(machine_id=machine_id)
        return qs

    def upsert_cost_rate(self, data: dict, user=None) -> BlowingCostRate:
        """Create-or-update the one active rate for (company, machine-or-global,
        category). A per-machine machine_id must belong to this company."""
        machine_id = data.get('machine_id')
        if machine_id is not None:
            self._get_machine_or_raise(machine_id)   # validates company ownership
        rate, _ = BlowingCostRate.objects.update_or_create(
            company=self.company,
            machine_id=machine_id,
            category=data['category'],
            is_active=True,
            defaults={
                'basis': data['basis'],
                'rate': data['rate'],
                'is_credit': data.get('is_credit', False),
                'label': data.get('label', ''),
                'updated_by': user,
            },
        )
        return rate

    def delete_cost_rate(self, rate_id: int) -> None:
        """Soft-delete (is_active=False) so the partial unique constraint frees up
        and a later upsert re-creates cleanly."""
        try:
            rate = BlowingCostRate.objects.get(id=rate_id, company=self.company)
        except BlowingCostRate.DoesNotExist:
            raise ValueError("Cost rate not found.")
        rate.is_active = False
        rate.save(update_fields=['is_active', 'updated_at'])

    # ==================================================================
    # Runs
    # ==================================================================
    def list_runs(self, date_from=None, date_to=None, machine_id=None, status=None):
        qs = (
            BlowingRun.objects
            .filter(company=self.company)
            .select_related('machine', 'preform_spec', 'cost_summary')
        )
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if machine_id:
            qs = qs.filter(machine_id=machine_id)
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_run(self, run_id) -> BlowingRun:
        try:
            return (
                BlowingRun.objects
                .select_related('machine', 'preform_spec', 'cost_summary', 'rate_config')
                .prefetch_related('segments', 'breakdowns')
                .get(id=run_id, company=self.company)
            )
        except BlowingRun.DoesNotExist:
            raise ValueError("Blowing run not found.")

    @transaction.atomic
    def create_run(self, data: dict, user=None) -> BlowingRun:
        machine = self._get_machine_or_raise(data['machine_id'])
        spec = self._get_spec_or_raise(data['preform_spec_id'])
        date = data['date']

        last = (
            BlowingRun.objects
            .filter(company=self.company, date=date)
            .order_by('-run_number')
            .first()
        )
        run_number = (last.run_number + 1) if last else 1

        run = BlowingRun(
            company=self.company,
            run_number=run_number,
            date=date,
            machine=machine,
            preform_spec=spec,
            status=data.get('status', RunStatus.DRAFT),
            sap_preform_item_code=spec.sap_item_code,
            created_by=user,
        )
        for field in RUN_INPUT_FIELDS:
            if field in data and data[field] is not None:
                setattr(run, field, data[field])
        # Per-bottle preform rate is snapshotted from the spec at creation.
        # Blowing-side rates now live in the Cost Master and resolve at compute time.
        run.preform_rate_per_bottle = spec.preform_rate_per_bottle or 0
        run.mould_cost = spec.mould_cost or 0
        run.mould_life_bottles = spec.mould_life_bottles or 0

        run.save()
        recalculate_run_cost(run)
        return run

    @transaction.atomic
    def update_run(self, run_id, data: dict, user=None) -> BlowingRun:
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("A completed run cannot be edited.")

        if 'machine_id' in data:
            run.machine = self._get_machine_or_raise(data['machine_id'])
            run.machine_depreciation_per_day = run.machine.depreciation_per_day
        if 'preform_spec_id' in data:
            run.preform_spec = self._get_spec_or_raise(data['preform_spec_id'])
            run.sap_preform_item_code = run.preform_spec.sap_item_code
            run.preform_rate_per_bottle = run.preform_spec.preform_rate_per_bottle or 0
            run.mould_cost = run.preform_spec.mould_cost or 0
            run.mould_life_bottles = run.preform_spec.mould_life_bottles or 0
        for field in RUN_INPUT_FIELDS:
            if field in data and data[field] is not None:
                setattr(run, field, data[field])
        if 'status' in data:
            run.status = data['status']
        run.updated_by = user
        run.save()
        recalculate_run_cost(run)
        return run

    # ==================================================================
    # Buy prices (Phase 2)
    # ==================================================================
    def list_buy_prices(self, is_active=None, preform_spec_id=None):
        qs = BottleBuyPrice.objects.filter(company=self.company).select_related('preform_spec')
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if preform_spec_id:
            qs = qs.filter(preform_spec_id=preform_spec_id)
        return qs

    def create_buy_price(self, data: dict, user=None) -> BottleBuyPrice:
        spec = self._get_spec_or_raise(data['preform_spec_id'])
        return BottleBuyPrice.objects.create(
            company=self.company, preform_spec=spec, created_by=user,
            **{k: data[k] for k in BUY_PRICE_FIELDS if k in data}
        )

    def update_buy_price(self, buy_id: int, data: dict, user=None) -> BottleBuyPrice:
        bp = self._get_buy_price_or_raise(buy_id)
        for field in [*BUY_PRICE_FIELDS, 'is_active']:
            if field in data:
                setattr(bp, field, data[field])
        bp.updated_by = user
        bp.save()
        return bp

    def _get_buy_price_or_raise(self, buy_id) -> BottleBuyPrice:
        try:
            return BottleBuyPrice.objects.get(id=buy_id, company=self.company)
        except BottleBuyPrice.DoesNotExist:
            raise ValueError("Buy price not found.")

    def get_effective_buy_price(self, preform_spec_id, date) -> BottleBuyPrice:
        return (
            BottleBuyPrice.objects
            .filter(company=self.company, is_active=True,
                    preform_spec_id=preform_spec_id, effective_from__lte=date)
            .order_by('-effective_from')
            .first()
        )

    @transaction.atomic
    def complete_run(self, run_id, data=None, user=None) -> BlowingRun:
        """Finalize the run — record end-of-run readings/production/manpower,
        guard that nothing is still running, flip to COMPLETED and cost it."""
        data = data or {}
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Run is already completed.")
        if run.segments.filter(is_active=True).exists():
            raise ValueError("Stop production before completing the run.")
        if run.breakdowns.filter(is_active=True).exists():
            raise ValueError("Resolve the active breakdown before completing the run.")

        for field in COMPLETION_FIELDS:
            if field in data and data[field] is not None:
                setattr(run, field, data[field])
        if run.total_counter_production <= 0:
            raise ValueError("Cannot complete a run with no production recorded.")

        self._recompute_run_totals(run)
        run.status = RunStatus.COMPLETED
        run.updated_by = user
        run.save()
        recalculate_run_cost(run)
        return run

    # ==================================================================
    # Run lifecycle — segments / breakdowns
    # ==================================================================
    def _recompute_run_totals(self, run: BlowingRun):
        """Running minutes from closed segments; breakdown minutes total. Mutates
        the run in place — callers save."""
        total_running = 0
        for seg in run.segments.filter(end_time__isnull=False):
            total_running += int((seg.end_time - seg.start_time).total_seconds() / 60)
        run.total_running_minutes = total_running
        run.total_breakdown_time = run.breakdowns.aggregate(t=Sum('breakdown_minutes'))['t'] or 0

    @transaction.atomic
    def start_production(self, run_id, user=None) -> BlowingSegment:
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot start a completed run.")
        # Warehouse approval gate (mirrors production_execution).
        if run.warehouse_approval_status == WarehouseApprovalStatus.NOT_REQUESTED:
            raise ValueError("Submit the preform request to warehouse before starting production.")
        if run.warehouse_approval_status == WarehouseApprovalStatus.PENDING:
            raise ValueError("Preform request is pending warehouse approval.")
        if run.warehouse_approval_status == WarehouseApprovalStatus.REJECTED:
            raise ValueError("Preform request was rejected by warehouse.")
        if run.segments.filter(is_active=True).exists():
            raise ValueError("Production is already running.")
        if run.breakdowns.filter(is_active=True).exists():
            raise ValueError("There is an active breakdown. Resolve it first.")

        segment = BlowingSegment.objects.create(
            blowing_run=run, start_time=timezone.now(), is_active=True,
        )
        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
            run.save(update_fields=['status', 'updated_at'])
        return segment

    @transaction.atomic
    def stop_production(self, run_id, produced_pcs, remarks='', user=None) -> BlowingSegment:
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot stop production on a completed run.")
        segment = run.segments.filter(is_active=True).first()
        if not segment:
            raise ValueError("No active running segment to stop.")
        from decimal import Decimal
        segment.end_time = timezone.now()
        segment.produced_pcs = Decimal(str(produced_pcs or 0))
        segment.remarks = remarks or ''
        segment.is_active = False
        segment.save()
        self._recompute_run_totals(run)
        run.save()
        return segment

    def _get_breakdown_category_or_raise(self, category_id) -> BlowingBreakdownCategory:
        try:
            return BlowingBreakdownCategory.objects.get(id=category_id, company=self.company)
        except BlowingBreakdownCategory.DoesNotExist:
            raise ValueError("Breakdown category not found.")

    @transaction.atomic
    def add_breakdown(self, run_id, data: dict, user=None) -> BlowingBreakdown:
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot add breakdowns to a completed run.")
        now = timezone.now()
        # Close any active running segment, recording pcs produced.
        segment = run.segments.filter(is_active=True).first()
        if segment:
            from decimal import Decimal
            segment.end_time = now
            segment.produced_pcs = Decimal(str(data.get('produced_pcs', 0) or 0))
            segment.is_active = False
            segment.save()

        machine = None
        if data.get('machine_id'):
            machine = self._get_machine_or_raise(data['machine_id'])
        category = None
        if data.get('breakdown_category_id'):
            category = self._get_breakdown_category_or_raise(data['breakdown_category_id'])

        breakdown = BlowingBreakdown.objects.create(
            blowing_run=run, machine=machine, start_time=now,
            breakdown_category=category, is_active=True,
            reason=data.get('reason', ''), remarks=data.get('remarks', ''),
        )
        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
            run.save(update_fields=['status', 'updated_at'])
        self._recompute_run_totals(run)
        run.save()
        return breakdown

    @transaction.atomic
    def resolve_breakdown(self, run_id, breakdown_id, action: str, user=None) -> BlowingBreakdown:
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot resolve breakdowns on a completed run.")
        try:
            breakdown = BlowingBreakdown.objects.get(
                id=breakdown_id, blowing_run=run, is_active=True)
        except BlowingBreakdown.DoesNotExist:
            raise ValueError("Active breakdown not found.")

        now = timezone.now()
        breakdown.end_time = now
        breakdown.breakdown_minutes = int((now - breakdown.start_time).total_seconds() / 60)
        breakdown.is_active = False
        if action == 'stop_unrecovered':
            breakdown.is_unrecovered = True
        breakdown.save()

        if action == 'start_production':
            BlowingSegment.objects.create(blowing_run=run, start_time=now, is_active=True)

        self._recompute_run_totals(run)
        run.save()
        return breakdown

    # ==================================================================
    # Manual (backfill) entries — user supplies start & end time
    # ==================================================================
    def _validate_manual_interval(self, run, start_time, end_time, kind: str):
        """Shared validation for manually backfilled segments/breakdowns."""
        if start_time is None or end_time is None:
            raise ValueError("Both start time and end time are required.")
        if end_time <= start_time:
            raise ValueError("End time must be after start time.")
        if start_time > timezone.now():
            raise ValueError("Start time cannot be in the future.")

        # Reject overlaps so run totals stay correct.
        for seg in run.segments.filter(end_time__isnull=False):
            if start_time < seg.end_time and seg.start_time < end_time:
                raise ValueError(
                    f"This {kind} overlaps an existing running segment "
                    f"({timezone.localtime(seg.start_time):%H:%M}–{timezone.localtime(seg.end_time):%H:%M})."
                )
        if run.segments.filter(is_active=True).exists():
            raise ValueError(
                "There is a live running segment in progress. Stop it before adding a manual entry."
            )
        for bd in run.breakdowns.filter(end_time__isnull=False):
            if start_time < bd.end_time and bd.start_time < end_time:
                raise ValueError(
                    f"This {kind} overlaps an existing breakdown "
                    f"({timezone.localtime(bd.start_time):%H:%M}–{timezone.localtime(bd.end_time):%H:%M})."
                )
        if run.breakdowns.filter(is_active=True).exists():
            raise ValueError(
                "There is a live breakdown in progress. Resolve it before adding a manual entry."
            )

    @transaction.atomic
    def add_manual_segment(self, run_id, data: dict, user=None) -> BlowingSegment:
        """Backfill a completed running segment with explicit start/end times."""
        from decimal import Decimal
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot add a running entry to a completed run.")

        start_time = data['start_time']
        end_time = data['end_time']
        self._validate_manual_interval(run, start_time, end_time, "running period")

        segment = BlowingSegment.objects.create(
            blowing_run=run,
            start_time=start_time,
            end_time=end_time,
            produced_pcs=Decimal(str(data.get('produced_pcs', 0) or 0)),
            is_active=False,
            remarks=data.get('remarks', ''),
        )
        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
        self._recompute_run_totals(run)
        run.save()
        return segment

    @transaction.atomic
    def add_manual_breakdown(self, run_id, data: dict, user=None) -> BlowingBreakdown:
        """Backfill a completed breakdown with explicit start/end times."""
        run = self.get_run(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot add a breakdown to a completed run.")

        start_time = data['start_time']
        end_time = data['end_time']
        self._validate_manual_interval(run, start_time, end_time, "breakdown")

        machine = None
        if data.get('machine_id'):
            machine = self._get_machine_or_raise(data['machine_id'])
        category = None
        if data.get('breakdown_category_id'):
            category = self._get_breakdown_category_or_raise(data['breakdown_category_id'])

        breakdown = BlowingBreakdown.objects.create(
            blowing_run=run,
            machine=machine,
            start_time=start_time,
            end_time=end_time,
            breakdown_minutes=int((end_time - start_time).total_seconds() / 60),
            breakdown_category=category,
            is_active=False,
            reason=data.get('reason', ''),
            remarks=data.get('remarks', ''),
        )
        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
        self._recompute_run_totals(run)
        run.save()
        return breakdown

    @transaction.atomic
    def update_segment(self, run_id, segment_id, data: dict) -> BlowingSegment:
        run = self.get_run(run_id)
        try:
            segment = BlowingSegment.objects.get(id=segment_id, blowing_run=run)
        except BlowingSegment.DoesNotExist:
            raise ValueError("Segment not found.")
        if 'produced_pcs' in data and data['produced_pcs'] is not None:
            from decimal import Decimal
            segment.produced_pcs = Decimal(str(data['produced_pcs']))
        if 'remarks' in data:
            segment.remarks = data['remarks'] or ''
        segment.save()
        return segment

    # ==================================================================
    # Breakdown categories (master data)
    # ==================================================================
    def list_breakdown_categories(self, is_active=None):
        qs = BlowingBreakdownCategory.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_breakdown_category(self, data: dict, user=None) -> BlowingBreakdownCategory:
        return BlowingBreakdownCategory.objects.create(
            company=self.company, name=data['name'], created_by=user)

    def update_breakdown_category(self, category_id, data: dict, user=None):
        cat = self._get_breakdown_category_or_raise(category_id)
        for field in ['name', 'is_active']:
            if field in data:
                setattr(cat, field, data[field])
        cat.updated_by = user
        cat.save()
        return cat

    # ==================================================================
    # Warehouse-approval gate — preform request
    # ==================================================================
    # The preform request is a real warehouse.BOMRequest so it lands in the
    # Warehouse → BOM Requests queue (same as production). Approval there flips
    # this run's warehouse_approval_status. See warehouse.WarehouseService.
    @transaction.atomic
    def submit_preform_request(self, run_id, remarks='', user=None):
        # ensure the run exists / is ours
        self.get_run(run_id)
        from warehouse.services.warehouse_service import WarehouseService
        return WarehouseService(self.company_code).create_blowing_bom_request(
            run_id, user=user, remarks=remarks)
