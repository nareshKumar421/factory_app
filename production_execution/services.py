import logging
from datetime import time
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    ProductionLine, Machine, MachineChecklistTemplate,
    ProductionRun, ProductionLog, MachineBreakdown,
    ProductionMaterialUsage, MachineRuntime, ProductionManpower,
    LineClearance, LineClearanceItem,
    MachineChecklistEntry, WasteLog,
    RunStatus, ClearanceStatus, WasteApprovalStatus,
    BreakdownType, ClearanceResult,
)

logger = logging.getLogger(__name__)

# Standard line clearance checklist items
STANDARD_CLEARANCE_ITEMS = [
    "Previous product, labels and packaging materials removed",
    "Machine/equipment cleaned and free from product residues",
    "Utensils, scoops and accessories cleaned and available",
    "Packaging area free from previous batch coding material",
    "Work area (tables, conveyors, floor) cleaned and sanitized",
    "Waste bins emptied and cleaned",
    "Required packaging material verified against BOM",
    "Coding machine updated with correct product/batch details",
    "Environmental conditions (temperature/humidity) within limits",
]

# Pre-defined hourly time slots (7:00 - 19:00)
HOURLY_TIME_SLOTS = [
    {"slot": "07:00-08:00", "start": time(7, 0), "end": time(8, 0)},
    {"slot": "08:00-09:00", "start": time(8, 0), "end": time(9, 0)},
    {"slot": "09:00-10:00", "start": time(9, 0), "end": time(10, 0)},
    {"slot": "10:00-11:00", "start": time(10, 0), "end": time(11, 0)},
    {"slot": "11:00-12:00", "start": time(11, 0), "end": time(12, 0)},
    {"slot": "12:00-13:00", "start": time(12, 0), "end": time(13, 0)},
    {"slot": "13:00-14:00", "start": time(13, 0), "end": time(14, 0)},
    {"slot": "14:00-15:00", "start": time(14, 0), "end": time(15, 0)},
    {"slot": "15:00-16:00", "start": time(15, 0), "end": time(16, 0)},
    {"slot": "16:00-17:00", "start": time(16, 0), "end": time(17, 0)},
    {"slot": "17:00-18:00", "start": time(17, 0), "end": time(18, 0)},
    {"slot": "18:00-19:00", "start": time(18, 0), "end": time(19, 0)},
]


class ProductionExecutionService:

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
    # MASTER DATA — Production Lines
    # ==================================================================

    def list_lines(self, is_active=None):
        qs = ProductionLine.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_line(self, data: dict) -> ProductionLine:
        return ProductionLine.objects.create(
            company=self.company,
            name=data['name'],
            description=data.get('description', ''),
        )

    def update_line(self, line_id: int, data: dict) -> ProductionLine:
        line = self._get_line_or_raise(line_id)
        for field in ['name', 'description', 'is_active']:
            if field in data:
                setattr(line, field, data[field])
        line.save()
        return line

    def delete_line(self, line_id: int):
        line = self._get_line_or_raise(line_id)
        line.is_active = False
        line.save(update_fields=['is_active', 'updated_at'])

    def _get_line_or_raise(self, line_id: int) -> ProductionLine:
        try:
            return ProductionLine.objects.get(id=line_id, company=self.company)
        except ProductionLine.DoesNotExist:
            raise ValueError(f"Production line {line_id} not found.")

    # ==================================================================
    # MASTER DATA — Machines
    # ==================================================================

    def list_machines(self, line_id=None, machine_type=None, is_active=None):
        qs = Machine.objects.filter(company=self.company).select_related('line')
        if line_id:
            qs = qs.filter(line_id=line_id)
        if machine_type:
            qs = qs.filter(machine_type=machine_type)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_machine(self, data: dict) -> Machine:
        line = self._get_line_or_raise(data['line_id'])
        return Machine.objects.create(
            company=self.company,
            name=data['name'],
            machine_type=data['machine_type'],
            line=line,
        )

    def update_machine(self, machine_id: int, data: dict) -> Machine:
        machine = self._get_machine_or_raise(machine_id)
        for field in ['name', 'machine_type', 'is_active']:
            if field in data:
                setattr(machine, field, data[field])
        if 'line_id' in data:
            machine.line = self._get_line_or_raise(data['line_id'])
        machine.save()
        return machine

    def delete_machine(self, machine_id: int):
        machine = self._get_machine_or_raise(machine_id)
        machine.is_active = False
        machine.save(update_fields=['is_active', 'updated_at'])

    def _get_machine_or_raise(self, machine_id: int) -> Machine:
        try:
            return Machine.objects.get(id=machine_id, company=self.company)
        except Machine.DoesNotExist:
            raise ValueError(f"Machine {machine_id} not found.")

    # ==================================================================
    # MASTER DATA — Checklist Templates
    # ==================================================================

    def list_checklist_templates(self, machine_type=None, frequency=None):
        qs = MachineChecklistTemplate.objects.filter(company=self.company)
        if machine_type:
            qs = qs.filter(machine_type=machine_type)
        if frequency:
            qs = qs.filter(frequency=frequency)
        return qs

    def create_checklist_template(self, data: dict) -> MachineChecklistTemplate:
        return MachineChecklistTemplate.objects.create(
            company=self.company,
            machine_type=data['machine_type'],
            task=data['task'],
            frequency=data['frequency'],
            sort_order=data.get('sort_order', 0),
        )

    def update_checklist_template(self, template_id: int, data: dict) -> MachineChecklistTemplate:
        template = self._get_template_or_raise(template_id)
        for field in ['machine_type', 'task', 'frequency', 'sort_order', 'is_active']:
            if field in data:
                setattr(template, field, data[field])
        template.save()
        return template

    def delete_checklist_template(self, template_id: int):
        template = self._get_template_or_raise(template_id)
        template.delete()

    def _get_template_or_raise(self, template_id: int) -> MachineChecklistTemplate:
        try:
            return MachineChecklistTemplate.objects.get(
                id=template_id, company=self.company
            )
        except MachineChecklistTemplate.DoesNotExist:
            raise ValueError(f"Checklist template {template_id} not found.")

    # ==================================================================
    # PRODUCTION RUNS
    # ==================================================================

    def list_runs(self, date=None, line_id=None, status=None, production_plan_id=None):
        qs = ProductionRun.objects.filter(
            company=self.company
        ).select_related('line', 'production_plan', 'created_by')
        if date:
            qs = qs.filter(date=date)
        if line_id:
            qs = qs.filter(line_id=line_id)
        if status:
            qs = qs.filter(status=status)
        if production_plan_id:
            qs = qs.filter(production_plan_id=production_plan_id)
        return qs

    @transaction.atomic
    def create_run(self, data: dict, user) -> ProductionRun:
        from production_planning.models import ProductionPlan, PlanStatus

        # Validate production plan
        try:
            plan = ProductionPlan.objects.get(
                id=data['production_plan_id'],
                company__code=self.company_code
            )
        except ProductionPlan.DoesNotExist:
            raise ValueError("Production plan not found.")

        if plan.status not in (PlanStatus.OPEN, PlanStatus.IN_PROGRESS):
            raise ValueError(
                f"Cannot start a run for a plan with status '{plan.status}'. "
                f"Plan must be OPEN or IN_PROGRESS."
            )

        # Validate line
        line = self._get_line_or_raise(data['line_id'])
        if not line.is_active:
            raise ValueError(f"Production line '{line.name}' is not active.")

        # Auto-increment run_number
        last_run = ProductionRun.objects.filter(
            company=self.company,
            production_plan=plan,
            date=data['date'],
        ).order_by('-run_number').first()
        run_number = (last_run.run_number + 1) if last_run else 1

        # Check line clearance (warning, not blocker)
        warnings = []
        clearance_exists = LineClearance.objects.filter(
            company=self.company,
            production_plan=plan,
            line=line,
            status=ClearanceStatus.CLEARED,
        ).exists()
        if not clearance_exists:
            warnings.append("No cleared line clearance found for this plan+line.")

        run = ProductionRun.objects.create(
            company=self.company,
            production_plan=plan,
            run_number=run_number,
            date=data['date'],
            line=line,
            brand=data.get('brand', ''),
            pack=data.get('pack', ''),
            sap_order_no=data.get('sap_order_no', ''),
            rated_speed=data.get('rated_speed'),
            status=RunStatus.DRAFT,
            created_by=user,
        )

        logger.info(f"Production run created: ID={run.id}, Run#{run_number}")
        return run

    def get_run(self, run_id: int) -> ProductionRun:
        return self._get_run_or_raise(run_id)

    def update_run(self, run_id: int, data: dict) -> ProductionRun:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit a COMPLETED run.")

        for field in ['brand', 'pack', 'sap_order_no', 'rated_speed']:
            if field in data:
                setattr(run, field, data[field])

        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
        run.save()
        return run

    @transaction.atomic
    def complete_run(self, run_id: int) -> ProductionRun:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Run is already completed.")

        self._recompute_run_totals(run)
        run.status = RunStatus.COMPLETED
        run.save()
        logger.info(f"Production run {run_id} completed. Total: {run.total_production}")
        return run

    def _recompute_run_totals(self, run: ProductionRun):
        # Sum hourly logs
        log_agg = run.logs.aggregate(
            total_prod=Sum('produced_cases'),
            total_pe=Sum('recd_minutes'),
        )
        run.total_production = log_agg['total_prod'] or 0
        run.total_minutes_pe = log_agg['total_pe'] or 0

        # Sum breakdowns
        bd_agg = run.breakdowns.aggregate(total=Sum('breakdown_minutes'))
        run.total_breakdown_time = bd_agg['total'] or 0

        line_bd = run.breakdowns.filter(
            type=BreakdownType.LINE
        ).aggregate(total=Sum('breakdown_minutes'))
        run.line_breakdown_time = line_bd['total'] or 0

        ext_bd = run.breakdowns.filter(
            type=BreakdownType.EXTERNAL
        ).aggregate(total=Sum('breakdown_minutes'))
        run.external_breakdown_time = ext_bd['total'] or 0

        # Unrecorded time (720 min for 12-hour shift)
        total_shift_minutes = 720
        run.unrecorded_time = max(
            0, total_shift_minutes - run.total_minutes_pe - run.total_breakdown_time
        )
        run.total_minutes_me = run.total_minutes_pe

    def _get_run_or_raise(self, run_id: int) -> ProductionRun:
        try:
            return ProductionRun.objects.select_related(
                'line', 'production_plan'
            ).prefetch_related(
                'logs', 'breakdowns'
            ).get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

    # ==================================================================
    # HOURLY PRODUCTION LOGS
    # ==================================================================

    def get_run_logs(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return run.logs.all()

    @transaction.atomic
    def save_hourly_logs(self, run_id: int, logs_data: list) -> list:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit logs of a COMPLETED run.")

        saved_logs = []
        for log_data in logs_data:
            log, created = ProductionLog.objects.update_or_create(
                production_run=run,
                time_start=log_data['time_start'],
                defaults={
                    'time_slot': log_data['time_slot'],
                    'time_end': log_data['time_end'],
                    'produced_cases': log_data.get('produced_cases', 0),
                    'machine_status': log_data.get('machine_status', 'RUNNING'),
                    'recd_minutes': log_data.get('recd_minutes', 0),
                    'breakdown_detail': log_data.get('breakdown_detail', ''),
                    'remarks': log_data.get('remarks', ''),
                },
            )
            saved_logs.append(log)

        # Recompute run totals after saving logs
        self._recompute_run_totals(run)
        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
        run.save()

        return saved_logs

    def update_log(self, run_id: int, log_id: int, data: dict) -> ProductionLog:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit logs of a COMPLETED run.")

        try:
            log = ProductionLog.objects.get(id=log_id, production_run=run)
        except ProductionLog.DoesNotExist:
            raise ValueError(f"Production log {log_id} not found.")

        for field in ['produced_cases', 'machine_status', 'recd_minutes',
                      'breakdown_detail', 'remarks']:
            if field in data:
                setattr(log, field, data[field])
        log.save()

        self._recompute_run_totals(run)
        run.save()
        return log

    # ==================================================================
    # MACHINE BREAKDOWNS
    # ==================================================================

    def get_run_breakdowns(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return run.breakdowns.select_related('machine').all()

    @transaction.atomic
    def add_breakdown(self, run_id: int, data: dict) -> MachineBreakdown:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot add breakdowns to a COMPLETED run.")

        machine = self._get_machine_or_raise(data['machine_id'])
        if machine.line_id != run.line_id:
            raise ValueError("Machine does not belong to the same line as the run.")

        # Auto-calculate breakdown_minutes if not provided
        breakdown_minutes = data.get('breakdown_minutes', 0)
        if data.get('end_time') and data['start_time'] and not breakdown_minutes:
            diff = data['end_time'] - data['start_time']
            breakdown_minutes = int(diff.total_seconds() / 60)

        breakdown = MachineBreakdown.objects.create(
            production_run=run,
            machine=machine,
            start_time=data['start_time'],
            end_time=data.get('end_time'),
            breakdown_minutes=breakdown_minutes,
            type=data['type'],
            is_unrecovered=data.get('is_unrecovered', False),
            reason=data['reason'],
            remarks=data.get('remarks', ''),
        )

        self._recompute_run_totals(run)
        run.save()
        return breakdown

    @transaction.atomic
    def update_breakdown(self, run_id: int, breakdown_id: int, data: dict) -> MachineBreakdown:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit breakdowns of a COMPLETED run.")

        try:
            breakdown = MachineBreakdown.objects.get(
                id=breakdown_id, production_run=run
            )
        except MachineBreakdown.DoesNotExist:
            raise ValueError(f"Breakdown {breakdown_id} not found.")

        if 'machine_id' in data:
            machine = self._get_machine_or_raise(data['machine_id'])
            if machine.line_id != run.line_id:
                raise ValueError("Machine does not belong to the same line as the run.")
            breakdown.machine = machine

        for field in ['start_time', 'end_time', 'breakdown_minutes', 'type',
                      'is_unrecovered', 'reason', 'remarks']:
            if field in data:
                setattr(breakdown, field, data[field])

        breakdown.save()
        self._recompute_run_totals(run)
        run.save()
        return breakdown

    @transaction.atomic
    def delete_breakdown(self, run_id: int, breakdown_id: int):
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot delete breakdowns from a COMPLETED run.")

        try:
            breakdown = MachineBreakdown.objects.get(
                id=breakdown_id, production_run=run
            )
        except MachineBreakdown.DoesNotExist:
            raise ValueError(f"Breakdown {breakdown_id} not found.")

        breakdown.delete()
        self._recompute_run_totals(run)
        run.save()

    # ==================================================================
    # MATERIAL USAGE (YIELD)
    # ==================================================================

    def get_run_materials(self, run_id: int, batch_number=None):
        run = self._get_run_or_raise(run_id)
        qs = run.material_usages.all()
        if batch_number:
            qs = qs.filter(batch_number=batch_number)
        return qs

    @transaction.atomic
    def save_material_usage(self, run_id: int, materials_data) -> list:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit materials of a COMPLETED run.")

        if not isinstance(materials_data, list):
            materials_data = [materials_data]

        saved = []
        for mat in materials_data:
            wastage_qty = mat.get('opening_qty', 0) + mat.get('issued_qty', 0) - mat.get('closing_qty', 0)
            usage = ProductionMaterialUsage.objects.create(
                production_run=run,
                material_code=mat.get('material_code', ''),
                material_name=mat['material_name'],
                opening_qty=mat.get('opening_qty', 0),
                issued_qty=mat.get('issued_qty', 0),
                closing_qty=mat.get('closing_qty', 0),
                wastage_qty=wastage_qty,
                uom=mat.get('uom', ''),
                batch_number=mat.get('batch_number', 1),
            )
            saved.append(usage)
        return saved

    def update_material(self, run_id: int, material_id: int, data: dict) -> ProductionMaterialUsage:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit materials of a COMPLETED run.")

        try:
            usage = ProductionMaterialUsage.objects.get(
                id=material_id, production_run=run
            )
        except ProductionMaterialUsage.DoesNotExist:
            raise ValueError(f"Material usage {material_id} not found.")

        from decimal import Decimal as D
        for field in ['material_code', 'material_name', 'uom', 'batch_number']:
            if field in data:
                setattr(usage, field, data[field])
        for field in ['opening_qty', 'issued_qty', 'closing_qty']:
            if field in data:
                setattr(usage, field, D(str(data[field])))

        # Ensure all qty fields are Decimal before calculation
        usage.opening_qty = D(str(usage.opening_qty))
        usage.issued_qty = D(str(usage.issued_qty))
        usage.closing_qty = D(str(usage.closing_qty))
        # Recalculate wastage
        usage.wastage_qty = usage.opening_qty + usage.issued_qty - usage.closing_qty
        usage.save()
        return usage

    # ==================================================================
    # MACHINE RUNTIME
    # ==================================================================

    def get_run_machine_runtime(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return run.machine_runtimes.all()

    @transaction.atomic
    def save_machine_runtime(self, run_id: int, runtime_data) -> list:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit machine runtime of a COMPLETED run.")

        if not isinstance(runtime_data, list):
            runtime_data = [runtime_data]

        saved = []
        for rt in runtime_data:
            machine = None
            if rt.get('machine_id'):
                machine = self._get_machine_or_raise(rt['machine_id'])

            runtime = MachineRuntime.objects.create(
                production_run=run,
                machine=machine,
                machine_type=rt['machine_type'],
                runtime_minutes=rt.get('runtime_minutes', 0),
                downtime_minutes=rt.get('downtime_minutes', 0),
                remarks=rt.get('remarks', ''),
            )
            saved.append(runtime)
        return saved

    def update_machine_runtime(self, run_id: int, runtime_id: int, data: dict) -> MachineRuntime:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit machine runtime of a COMPLETED run.")

        try:
            runtime = MachineRuntime.objects.get(id=runtime_id, production_run=run)
        except MachineRuntime.DoesNotExist:
            raise ValueError(f"Machine runtime {runtime_id} not found.")

        for field in ['machine_type', 'runtime_minutes', 'downtime_minutes', 'remarks']:
            if field in data:
                setattr(runtime, field, data[field])
        runtime.save()
        return runtime

    # ==================================================================
    # MANPOWER
    # ==================================================================

    def get_run_manpower(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return run.manpower_entries.all()

    def save_manpower(self, run_id: int, data: dict) -> ProductionManpower:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit manpower of a COMPLETED run.")

        manpower, created = ProductionManpower.objects.update_or_create(
            production_run=run,
            shift=data['shift'],
            defaults={
                'worker_count': data.get('worker_count', 0),
                'supervisor': data.get('supervisor', ''),
                'engineer': data.get('engineer', ''),
                'remarks': data.get('remarks', ''),
            },
        )
        return manpower

    def update_manpower(self, run_id: int, manpower_id: int, data: dict) -> ProductionManpower:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit manpower of a COMPLETED run.")

        try:
            manpower = ProductionManpower.objects.get(
                id=manpower_id, production_run=run
            )
        except ProductionManpower.DoesNotExist:
            raise ValueError(f"Manpower entry {manpower_id} not found.")

        for field in ['shift', 'worker_count', 'supervisor', 'engineer', 'remarks']:
            if field in data:
                setattr(manpower, field, data[field])
        manpower.save()
        return manpower

    # ==================================================================
    # LINE CLEARANCE
    # ==================================================================

    def list_clearances(self, date=None, line_id=None, status=None):
        qs = LineClearance.objects.filter(
            company=self.company
        ).select_related('line', 'created_by')
        if date:
            qs = qs.filter(date=date)
        if line_id:
            qs = qs.filter(line_id=line_id)
        if status:
            qs = qs.filter(status=status)
        return qs

    @transaction.atomic
    def create_clearance(self, data: dict, user) -> LineClearance:
        from production_planning.models import ProductionPlan

        line = self._get_line_or_raise(data['line_id'])

        try:
            plan = ProductionPlan.objects.get(
                id=data['production_plan_id'],
                company__code=self.company_code
            )
        except ProductionPlan.DoesNotExist:
            raise ValueError("Production plan not found.")

        clearance = LineClearance.objects.create(
            company=self.company,
            date=data['date'],
            line=line,
            production_plan=plan,
            document_id=data.get('document_id', ''),
            status=ClearanceStatus.DRAFT,
            created_by=user,
        )

        # Auto-create 9 standard checklist items
        for i, checkpoint in enumerate(STANDARD_CLEARANCE_ITEMS, 1):
            LineClearanceItem.objects.create(
                clearance=clearance,
                checkpoint=checkpoint,
                sort_order=i,
            )

        logger.info(f"Line clearance created: ID={clearance.id}")
        return clearance

    def get_clearance(self, clearance_id: int) -> LineClearance:
        try:
            return LineClearance.objects.select_related(
                'line', 'created_by', 'verified_by', 'qa_approved_by'
            ).prefetch_related('items').get(
                id=clearance_id, company=self.company
            )
        except LineClearance.DoesNotExist:
            raise ValueError(f"Line clearance {clearance_id} not found.")

    @transaction.atomic
    def update_clearance(self, clearance_id: int, data: dict) -> LineClearance:
        clearance = self.get_clearance(clearance_id)
        if clearance.status != ClearanceStatus.DRAFT:
            raise ValueError("Only DRAFT clearances can be edited.")

        # Update items
        if 'items' in data:
            for item_data in data['items']:
                try:
                    item = LineClearanceItem.objects.get(
                        id=item_data['id'], clearance=clearance
                    )
                    item.result = item_data['result']
                    item.remarks = item_data.get('remarks', '')
                    item.save()
                except LineClearanceItem.DoesNotExist:
                    pass

        # Update signatures
        if 'production_supervisor_sign' in data:
            clearance.production_supervisor_sign = data['production_supervisor_sign']
        if 'production_incharge_sign' in data:
            clearance.production_incharge_sign = data['production_incharge_sign']

        clearance.save()
        return clearance

    def submit_clearance(self, clearance_id: int) -> LineClearance:
        clearance = self.get_clearance(clearance_id)
        if clearance.status != ClearanceStatus.DRAFT:
            raise ValueError("Only DRAFT clearances can be submitted.")

        # Validate all items have a result
        items = clearance.items.all()
        for item in items:
            if item.result == ClearanceResult.NA:
                raise ValueError(
                    f"All checklist items must have a result. "
                    f"Item '{item.checkpoint}' is still N/A."
                )

        # At least one signature
        if not clearance.production_supervisor_sign and not clearance.production_incharge_sign:
            raise ValueError("At least one signature (supervisor or incharge) is required.")

        clearance.status = ClearanceStatus.SUBMITTED
        clearance.save(update_fields=['status', 'updated_at'])
        return clearance

    def approve_clearance(self, clearance_id: int, user, approved: bool) -> LineClearance:
        clearance = self.get_clearance(clearance_id)
        if clearance.status != ClearanceStatus.SUBMITTED:
            raise ValueError("Only SUBMITTED clearances can be approved/rejected.")

        if approved:
            clearance.status = ClearanceStatus.CLEARED
            clearance.qa_approved = True
        else:
            clearance.status = ClearanceStatus.NOT_CLEARED
            clearance.qa_approved = False

        clearance.qa_approved_by = user
        clearance.qa_approved_at = timezone.now()
        clearance.save()
        return clearance

    # ==================================================================
    # MACHINE CHECKLISTS
    # ==================================================================

    def list_checklist_entries(self, machine_id=None, month=None, year=None, frequency=None):
        qs = MachineChecklistEntry.objects.filter(
            company=self.company
        ).select_related('machine', 'template')
        if machine_id:
            qs = qs.filter(machine_id=machine_id)
        if month:
            qs = qs.filter(month=month)
        if year:
            qs = qs.filter(year=year)
        if frequency:
            qs = qs.filter(frequency=frequency)
        return qs

    def create_checklist_entry(self, data: dict) -> MachineChecklistEntry:
        machine = self._get_machine_or_raise(data['machine_id'])
        template = self._get_template_or_raise(data['template_id'])

        entry_date = data['date']
        return MachineChecklistEntry.objects.create(
            company=self.company,
            machine=machine,
            machine_type=machine.machine_type,
            date=entry_date,
            month=entry_date.month,
            year=entry_date.year,
            template=template,
            task_description=template.task,
            frequency=template.frequency,
            status=data.get('status', 'NA'),
            operator=data.get('operator', ''),
            shift_incharge=data.get('shift_incharge', ''),
            remarks=data.get('remarks', ''),
        )

    @transaction.atomic
    def bulk_upsert_checklist_entries(self, entries_data: list) -> list:
        saved = []
        for entry_data in entries_data:
            machine = self._get_machine_or_raise(entry_data['machine_id'])
            template = self._get_template_or_raise(entry_data['template_id'])
            entry_date = entry_data['date']

            entry, created = MachineChecklistEntry.objects.update_or_create(
                machine=machine,
                template=template,
                date=entry_date,
                defaults={
                    'company': self.company,
                    'machine_type': machine.machine_type,
                    'month': entry_date.month,
                    'year': entry_date.year,
                    'task_description': template.task,
                    'frequency': template.frequency,
                    'status': entry_data.get('status', 'NA'),
                    'operator': entry_data.get('operator', ''),
                    'shift_incharge': entry_data.get('shift_incharge', ''),
                    'remarks': entry_data.get('remarks', ''),
                },
            )
            saved.append(entry)
        return saved

    def update_checklist_entry(self, entry_id: int, data: dict) -> MachineChecklistEntry:
        try:
            entry = MachineChecklistEntry.objects.get(
                id=entry_id, company=self.company
            )
        except MachineChecklistEntry.DoesNotExist:
            raise ValueError(f"Checklist entry {entry_id} not found.")

        for field in ['status', 'operator', 'shift_incharge', 'remarks']:
            if field in data:
                setattr(entry, field, data[field])
        entry.save()
        return entry

    # ==================================================================
    # WASTE MANAGEMENT
    # ==================================================================

    def list_waste_logs(self, run_id=None, approval_status=None):
        qs = WasteLog.objects.filter(
            production_run__company=self.company
        ).select_related('production_run')
        if run_id:
            qs = qs.filter(production_run_id=run_id)
        if approval_status:
            qs = qs.filter(wastage_approval_status=approval_status)
        return qs

    def create_waste_log(self, data: dict) -> WasteLog:
        run = self._get_run_or_raise(data['production_run_id'])
        return WasteLog.objects.create(
            production_run=run,
            material_code=data.get('material_code', ''),
            material_name=data['material_name'],
            wastage_qty=data['wastage_qty'],
            uom=data.get('uom', ''),
            reason=data.get('reason', ''),
        )

    def get_waste_log(self, waste_id: int) -> WasteLog:
        try:
            return WasteLog.objects.select_related(
                'production_run',
                'engineer_signed_by', 'am_signed_by',
                'store_signed_by', 'hod_signed_by',
            ).get(id=waste_id, production_run__company=self.company)
        except WasteLog.DoesNotExist:
            raise ValueError(f"Waste log {waste_id} not found.")

    def approve_waste(self, waste_id: int, level: str, user, sign: str) -> WasteLog:
        waste = self.get_waste_log(waste_id)
        now = timezone.now()

        if level == 'engineer':
            waste.engineer_sign = sign
            waste.engineer_signed_by = user
            waste.engineer_signed_at = now
            waste.wastage_approval_status = WasteApprovalStatus.PARTIALLY_APPROVED

        elif level == 'am':
            if not waste.engineer_signed_at:
                raise ValueError("Engineer must sign before AM.")
            waste.am_sign = sign
            waste.am_signed_by = user
            waste.am_signed_at = now

        elif level == 'store':
            if not waste.am_signed_at:
                raise ValueError("AM must sign before Store.")
            waste.store_sign = sign
            waste.store_signed_by = user
            waste.store_signed_at = now

        elif level == 'hod':
            if not waste.store_signed_at:
                raise ValueError("Store must sign before HOD.")
            waste.hod_sign = sign
            waste.hod_signed_by = user
            waste.hod_signed_at = now
            waste.wastage_approval_status = WasteApprovalStatus.FULLY_APPROVED

        else:
            raise ValueError(f"Invalid approval level: {level}")

        waste.save()
        return waste

    # ==================================================================
    # REPORTS
    # ==================================================================

    def get_daily_production_report(self, date, line_id=None):
        qs = ProductionRun.objects.filter(
            company=self.company, date=date
        ).select_related('line', 'production_plan').prefetch_related(
            'logs', 'breakdowns', 'breakdowns__machine'
        )
        if line_id:
            qs = qs.filter(line_id=line_id)
        return qs

    def get_yield_report(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return {
            'run': run,
            'materials': run.material_usages.all(),
            'machine_runtimes': run.machine_runtimes.all(),
            'manpower': run.manpower_entries.all(),
        }

    def get_line_clearance_report(self, date_from=None, date_to=None):
        qs = LineClearance.objects.filter(
            company=self.company
        ).select_related('line', 'created_by')
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        return qs

    def get_analytics(self, date_from=None, date_to=None, line_id=None):
        qs = ProductionRun.objects.filter(
            company=self.company, status=RunStatus.COMPLETED
        )
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if line_id:
            qs = qs.filter(line_id=line_id)

        agg = qs.aggregate(
            total_production=Sum('total_production'),
            total_pe_minutes=Sum('total_minutes_pe'),
            total_breakdown=Sum('total_breakdown_time'),
            total_line_breakdown=Sum('line_breakdown_time'),
            total_external_breakdown=Sum('external_breakdown_time'),
        )

        total_runs = qs.count()
        total_production = agg['total_production'] or 0
        total_pe = agg['total_pe_minutes'] or 0
        total_breakdown = agg['total_breakdown'] or 0

        total_shift_minutes = total_runs * 720
        available_time = total_shift_minutes
        operating_time = max(0, available_time - total_breakdown)

        availability = (operating_time / available_time * 100) if available_time else 0

        return {
            'total_runs': total_runs,
            'total_production': total_production,
            'total_pe_minutes': total_pe,
            'total_breakdown_minutes': total_breakdown,
            'total_line_breakdown_minutes': agg['total_line_breakdown'] or 0,
            'total_external_breakdown_minutes': agg['total_external_breakdown'] or 0,
            'available_time_minutes': available_time,
            'operating_time_minutes': operating_time,
            'availability_percent': round(availability, 1),
        }
