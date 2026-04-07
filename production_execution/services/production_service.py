import logging
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    ProductionLine, Machine, MachineChecklistTemplate,
    BreakdownCategory,
    ProductionRun, ProductionSegment, MachineBreakdown,
    ProductionMaterialUsage, MachineRuntime, ProductionManpower,
    LineClearance, LineClearanceItem,
    MachineChecklistEntry, WasteLog,
    RunStatus, ClearanceStatus, WasteApprovalStatus,
    ClearanceResult,
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
    # MASTER DATA — Breakdown Categories
    # ==================================================================

    def list_breakdown_categories(self, is_active=None):
        qs = BreakdownCategory.objects.filter(company=self.company)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create_breakdown_category(self, data: dict) -> BreakdownCategory:
        return BreakdownCategory.objects.create(
            company=self.company,
            name=data['name'],
        )

    def update_breakdown_category(self, category_id: int, data: dict) -> BreakdownCategory:
        category = self._get_breakdown_category_or_raise(category_id)
        for field in ['name', 'is_active']:
            if field in data:
                setattr(category, field, data[field])
        category.save()
        return category

    def delete_breakdown_category(self, category_id: int):
        category = self._get_breakdown_category_or_raise(category_id)
        category.is_active = False
        category.save(update_fields=['is_active', 'updated_at'])

    def _get_breakdown_category_or_raise(self, category_id: int) -> BreakdownCategory:
        try:
            return BreakdownCategory.objects.get(id=category_id, company=self.company)
        except BreakdownCategory.DoesNotExist:
            raise ValueError(f"Breakdown category {category_id} not found.")

    # ==================================================================
    # PRODUCTION RUNS
    # ==================================================================

    def list_runs(self, date=None, date_from=None, date_to=None,
                  line_id=None, status=None, sap_doc_entry=None, search=None):
        qs = ProductionRun.objects.filter(
            company=self.company
        ).select_related('line', 'created_by')
        if date:
            qs = qs.filter(date=date)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if line_id:
            qs = qs.filter(line_id=line_id)
        if status:
            qs = qs.filter(status=status)
        if sap_doc_entry:
            qs = qs.filter(sap_doc_entry=sap_doc_entry)
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(product__icontains=search) |
                Q(run_number__icontains=search) |
                Q(sap_doc_entry__icontains=search)
            )
        return qs

    @transaction.atomic
    def create_run(self, data: dict, user) -> ProductionRun:
        line = self._get_line_or_raise(data['line_id'])
        if not line.is_active:
            raise ValueError(f"Production line '{line.name}' is not active.")

        sap_doc_entry = data.get('sap_doc_entry')

        last_run = ProductionRun.objects.filter(
            company=self.company,
            date=data['date'],
        ).order_by('-run_number').first()
        run_number = (last_run.run_number + 1) if last_run else 1

        run = ProductionRun.objects.create(
            company=self.company,
            sap_doc_entry=sap_doc_entry,
            run_number=run_number,
            date=data['date'],
            line=line,
            product=data.get('product', ''),
            required_qty=data.get('required_qty'),
            rated_speed=data.get('rated_speed'),
            labour_count=data.get('labour_count', 0),
            other_manpower_count=data.get('other_manpower_count', 0),
            supervisor=data.get('supervisor', ''),
            operators=data.get('operators', ''),
            status=RunStatus.DRAFT,
            created_by=user,
        )

        # Set machines M2M
        machine_ids = data.get('machine_ids', [])
        if machine_ids:
            machines = Machine.objects.filter(id__in=machine_ids, company=self.company)
            run.machines.set(machines)

        # Create initial materials — manual takes priority, otherwise auto-fetch BOM
        materials_data = data.get('materials', [])
        if materials_data:
            self.save_material_usage(run.id, materials_data)
        else:
            try:
                self.auto_populate_materials_from_bom(run)
            except Exception as e:
                logger.warning(f"Could not auto-fetch BOM for run {run.id}: {e}")

        logger.info(f"Production run created: ID={run.id}, Run#{run_number}")
        return run

    def auto_populate_materials_from_bom(self, run: ProductionRun) -> list:
        """
        Fetch BOM components from SAP and create ProductionMaterialUsage records.
        Priority: sap_doc_entry (WOR1) > product item code (OITT/ITT1).
        """
        from .sap_reader import ProductionOrderReader, SAPReadError
        from decimal import Decimal as D

        reader = ProductionOrderReader(self.company_code)
        components = reader.get_bom_components_for_run(
            sap_doc_entry=run.sap_doc_entry,
            item_code=run.product or None,
        )

        if not components:
            logger.info(f"No BOM components found for run {run.id}")
            return []

        saved = []
        for comp in components:
            opening = D(str(comp.get('PlannedQty') or 0))
            issued = D(str(comp.get('IssuedQty') or 0))
            usage = ProductionMaterialUsage.objects.create(
                production_run=run,
                material_code=comp.get('ItemCode') or '',
                material_name=comp.get('ItemName') or '',
                opening_qty=opening,
                issued_qty=issued,
                closing_qty=0,
                wastage_qty=opening + issued,
                uom=comp.get('UomCode') or '',
            )
            saved.append(usage)

        logger.info(f"Auto-populated {len(saved)} BOM materials for run {run.id}")
        return saved

    def get_run(self, run_id: int) -> ProductionRun:
        return self._get_run_or_raise(run_id)

    def update_run(self, run_id: int, data: dict) -> ProductionRun:
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot edit a COMPLETED run.")

        for field in ['product', 'rated_speed', 'labour_count', 'other_manpower_count',
                      'supervisor', 'operators']:
            if field in data:
                setattr(run, field, data[field])

        if 'machine_ids' in data:
            machines = Machine.objects.filter(id__in=data['machine_ids'], company=self.company)
            run.machines.set(machines)

        run.save()
        return run

    def delete_run(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot delete a COMPLETED run.")
        run.delete()

    # ==================================================================
    # TIMELINE FLOW — Start, Add Breakdown, Resolve, Complete
    # ==================================================================

    @transaction.atomic
    def start_production(self, run_id: int) -> ProductionSegment:
        """Start production — creates first running segment."""
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot start a COMPLETED run.")

        # Warehouse approval gate — only allow start if approved
        if run.warehouse_approval_status == 'PENDING':
            raise ValueError("Cannot start production — BOM request is pending warehouse approval.")
        if run.warehouse_approval_status == 'REJECTED':
            raise ValueError("Cannot start production — BOM request was rejected by warehouse.")

        if run.segments.filter(is_active=True).exists():
            raise ValueError("Production is already running.")
        if run.breakdowns.filter(is_active=True).exists():
            raise ValueError("There is an active breakdown. Resolve it first.")

        now = timezone.now()
        segment = ProductionSegment.objects.create(
            production_run=run,
            start_time=now,
            is_active=True,
        )

        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
            run.save(update_fields=['status', 'updated_at'])

        logger.info(f"Production started for run {run_id}")
        return segment

    @transaction.atomic
    def stop_production(self, run_id: int, produced_cases, remarks='') -> ProductionSegment:
        """Stop the active running segment and record cases produced."""
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot stop production on a COMPLETED run.")

        active_segment = run.segments.filter(is_active=True).first()
        if not active_segment:
            raise ValueError("No active running segment to stop.")

        from decimal import Decimal
        now = timezone.now()
        active_segment.end_time = now
        active_segment.produced_cases = Decimal(str(produced_cases))
        active_segment.remarks = remarks
        active_segment.is_active = False
        active_segment.save()

        self._recompute_run_totals(run)
        run.save()

        logger.info(f"Production stopped for run {run_id}")
        return active_segment

    @transaction.atomic
    def add_breakdown(self, run_id: int, data: dict) -> MachineBreakdown:
        """Close current running segment and create a breakdown."""
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot add breakdowns to a COMPLETED run.")

        now = timezone.now()

        # Close active running segment
        active_segment = run.segments.filter(is_active=True).first()
        if active_segment:
            active_segment.end_time = now
            active_segment.produced_cases = data.get('produced_cases', 0)
            active_segment.is_active = False
            active_segment.save()

        # Validate machine and category
        machine = self._get_machine_or_raise(data['machine_id'])
        category = self._get_breakdown_category_or_raise(data['breakdown_category_id'])

        breakdown = MachineBreakdown.objects.create(
            production_run=run,
            machine=machine,
            start_time=now,
            breakdown_category=category,
            is_active=True,
            reason=data['reason'],
            remarks=data.get('remarks', ''),
        )

        if run.status == RunStatus.DRAFT:
            run.status = RunStatus.IN_PROGRESS
            run.save(update_fields=['status', 'updated_at'])

        self._recompute_run_totals(run)
        logger.info(f"Breakdown added for run {run_id}: {category.name}")
        return breakdown

    @transaction.atomic
    def resolve_breakdown(self, run_id: int, breakdown_id: int, action: str) -> MachineBreakdown:
        """Resolve an active breakdown."""
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Cannot resolve breakdowns on a COMPLETED run.")

        try:
            breakdown = MachineBreakdown.objects.get(
                id=breakdown_id, production_run=run, is_active=True
            )
        except MachineBreakdown.DoesNotExist:
            raise ValueError(f"Active breakdown {breakdown_id} not found.")

        now = timezone.now()
        breakdown.end_time = now
        breakdown.breakdown_minutes = int(
            (now - breakdown.start_time).total_seconds() / 60
        )
        breakdown.is_active = False

        if action == 'stop_unrecovered':
            breakdown.is_unrecovered = True

        breakdown.save()

        # If "Fixed, Start Production", create a new running segment
        if action == 'start_production':
            ProductionSegment.objects.create(
                production_run=run,
                start_time=now,
                is_active=True,
            )

        self._recompute_run_totals(run)
        logger.info(f"Breakdown {breakdown_id} resolved with action '{action}'")
        return breakdown

    @transaction.atomic
    def complete_run(self, run_id: int, total_production) -> ProductionRun:
        """Complete the run. All segments and breakdowns must be closed first."""
        run = self._get_run_or_raise(run_id)
        if run.status == RunStatus.COMPLETED:
            raise ValueError("Run is already completed.")

        if run.segments.filter(is_active=True).exists():
            raise ValueError("Cannot complete run while production is running. Stop production first.")
        if run.breakdowns.filter(is_active=True).exists():
            raise ValueError("Cannot complete run while a breakdown is active. Resolve it first.")

        from decimal import Decimal
        run.total_production = Decimal(str(total_production))
        self._recompute_run_totals(run)
        run.status = RunStatus.COMPLETED
        run.save()

        logger.info(f"Production run {run_id} completed. Total: {run.total_production}")

        # Post goods receipt to SAP if linked to a SAP production order
        if run.sap_doc_entry:
            self._post_goods_receipt_to_sap(run)

        return run

    def _post_goods_receipt_to_sap(self, run: ProductionRun):
        """Post a goods receipt to SAP B1 for the completed production run."""
        from .sap_reader import ProductionOrderReader, SAPReadError
        from .sap_writer import GoodsReceiptWriter, SAPWriteError

        run.sap_sync_status = 'PENDING'
        run.sap_sync_error = ''
        run.save(update_fields=['sap_sync_status', 'sap_sync_error'])

        try:
            # Fetch ItemCode and Warehouse from SAP production order
            reader = ProductionOrderReader(self.company_code)
            order_detail = reader.get_production_order_detail(run.sap_doc_entry)
            header = order_detail.get('header', {})
            item_code = header.get('ItemCode')
            warehouse = header.get('Warehouse')

            if not item_code or not warehouse:
                raise SAPWriteError(
                    f"Missing ItemCode or Warehouse from SAP order {run.sap_doc_entry}"
                )

            # Calculate quantity to post (total produced minus rejected)
            qty = float(run.total_production - run.rejected_qty)
            if qty <= 0:
                run.sap_sync_status = 'NOT_APPLICABLE'
                run.sap_sync_error = 'Net production quantity is zero or negative, skipping SAP post.'
                run.save(update_fields=['sap_sync_status', 'sap_sync_error'])
                logger.info(f"Run {run.id}: net qty <= 0, skipping SAP goods receipt.")
                return

            # Post goods receipt
            writer = GoodsReceiptWriter(self.company_code)
            doc_entry = writer.post_goods_receipt(
                doc_entry=run.sap_doc_entry,
                item_code=item_code,
                warehouse=warehouse,
                qty=qty,
                posting_date=run.date,
            )

            run.sap_receipt_doc_entry = doc_entry
            run.sap_sync_status = 'SUCCESS'
            run.sap_sync_error = ''
            run.save(update_fields=['sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error'])
            logger.info(f"Run {run.id}: SAP goods receipt posted successfully. GR DocEntry={doc_entry}")

        except (SAPReadError, SAPWriteError) as e:
            run.sap_sync_status = 'FAILED'
            run.sap_sync_error = str(e)
            run.save(update_fields=['sap_sync_status', 'sap_sync_error'])
            logger.error(f"Run {run.id}: SAP goods receipt failed — {e}")

        except Exception as e:
            run.sap_sync_status = 'FAILED'
            run.sap_sync_error = f"Unexpected error: {e}"
            run.save(update_fields=['sap_sync_status', 'sap_sync_error'])
            logger.exception(f"Run {run.id}: Unexpected error posting SAP goods receipt")

    def retry_sap_goods_receipt(self, run_id: int) -> ProductionRun:
        """Retry posting goods receipt to SAP for a failed run."""
        run = self._get_run_or_raise(run_id)
        if run.status != RunStatus.COMPLETED:
            raise ValueError("Can only retry SAP sync for completed runs.")
        if run.sap_sync_status == 'SUCCESS':
            raise ValueError("SAP goods receipt already posted successfully.")
        if not run.sap_doc_entry:
            raise ValueError("Run is not linked to a SAP production order.")

        self._post_goods_receipt_to_sap(run)
        run.refresh_from_db()
        return run

    def _recompute_run_totals(self, run: ProductionRun):
        """Recompute running minutes and breakdown minutes from child records."""
        total_running = 0
        for seg in run.segments.filter(end_time__isnull=False):
            total_running += int((seg.end_time - seg.start_time).total_seconds() / 60)
        run.total_running_minutes = total_running

        bd_agg = run.breakdowns.aggregate(total=Sum('breakdown_minutes'))
        run.total_breakdown_time = bd_agg['total'] or 0

    def get_timeline(self, run_id: int):
        """Get merged timeline of segments and breakdowns, sorted by start_time."""
        run = self._get_run_or_raise(run_id)
        segments = list(run.segments.all())
        breakdowns = list(
            run.breakdowns.select_related('machine', 'breakdown_category').all()
        )
        return {
            'segments': segments,
            'breakdowns': breakdowns,
        }

    def _get_run_or_raise(self, run_id: int) -> ProductionRun:
        try:
            return ProductionRun.objects.select_related(
                'line'
            ).prefetch_related(
                'segments', 'breakdowns'
            ).get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

    # ==================================================================
    # SEGMENT & BREAKDOWN UPDATES (remarks, produced_cases)
    # ==================================================================

    def update_segment(self, run_id: int, segment_id: int, data: dict) -> ProductionSegment:
        run = self._get_run_or_raise(run_id)
        try:
            segment = ProductionSegment.objects.get(id=segment_id, production_run=run)
        except ProductionSegment.DoesNotExist:
            raise ValueError(f"Segment {segment_id} not found.")

        if 'remarks' in data:
            segment.remarks = data['remarks']
        if 'produced_cases' in data and not segment.is_active:
            from decimal import Decimal
            segment.produced_cases = Decimal(str(data['produced_cases']))
        segment.save()

        self._recompute_run_totals(run)
        run.save()
        return segment

    def update_breakdown_remarks(self, run_id: int, breakdown_id: int, remarks: str) -> MachineBreakdown:
        run = self._get_run_or_raise(run_id)
        try:
            breakdown = MachineBreakdown.objects.get(id=breakdown_id, production_run=run)
        except MachineBreakdown.DoesNotExist:
            raise ValueError(f"Breakdown {breakdown_id} not found.")

        breakdown.remarks = remarks
        breakdown.save(update_fields=['remarks', 'updated_at'])
        return breakdown

    # ==================================================================
    # LEGACY BREAKDOWN CRUD (for direct edits)
    # ==================================================================

    def get_run_breakdowns(self, run_id: int):
        run = self._get_run_or_raise(run_id)
        return run.breakdowns.select_related('machine', 'breakdown_category').all()

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
            breakdown.machine = self._get_machine_or_raise(data['machine_id'])
        if 'breakdown_category_id' in data:
            breakdown.breakdown_category = self._get_breakdown_category_or_raise(
                data['breakdown_category_id']
            )
        for field in ['start_time', 'end_time', 'breakdown_minutes',
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

        from decimal import Decimal as D
        saved = []
        for mat in materials_data:
            closing = D(str(mat.get('closing_qty') or 0))
            opening = D(str(mat.get('opening_qty', 0)))
            issued = D(str(mat.get('issued_qty', 0)))
            wastage_qty = opening + issued - closing
            usage = ProductionMaterialUsage.objects.create(
                production_run=run,
                material_code=mat.get('material_code', ''),
                material_name=mat['material_name'],
                opening_qty=opening,
                issued_qty=issued,
                closing_qty=closing,
                wastage_qty=wastage_qty,
                uom=mat.get('uom', ''),
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
        for field in ['material_code', 'material_name', 'uom']:
            if field in data:
                setattr(usage, field, data[field])
        for field in ['opening_qty', 'issued_qty', 'closing_qty']:
            if field in data:
                setattr(usage, field, D(str(data[field])))

        usage.opening_qty = D(str(usage.opening_qty))
        usage.issued_qty = D(str(usage.issued_qty))
        usage.closing_qty = D(str(usage.closing_qty))
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

    def list_clearances(self, date=None, line_id=None, status=None, production_run_id=None):
        qs = LineClearance.objects.filter(
            company=self.company
        ).select_related('line', 'created_by', 'production_run')
        if date:
            qs = qs.filter(date=date)
        if line_id:
            qs = qs.filter(line_id=line_id)
        if status:
            qs = qs.filter(status=status)
        if production_run_id:
            qs = qs.filter(production_run_id=production_run_id)
        return qs

    @transaction.atomic
    def create_clearance(self, data: dict, user) -> LineClearance:
        line = self._get_line_or_raise(data['line_id'])

        production_run = None
        if data.get('production_run_id'):
            production_run = self._get_run_or_raise(data['production_run_id'])

        clearance = LineClearance.objects.create(
            company=self.company,
            production_run=production_run,
            date=data['date'],
            line=line,
            document_id=data.get('document_id', ''),
            status=ClearanceStatus.DRAFT,
            created_by=user,
        )

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
                'line', 'created_by', 'verified_by', 'qa_approved_by', 'production_run'
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

        items = clearance.items.all()
        for item in items:
            if item.result == ClearanceResult.NA:
                raise ValueError(
                    f"All checklist items must have a result. "
                    f"Item '{item.checkpoint}' is still N/A."
                )

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
        ).select_related('line').prefetch_related(
            'segments', 'breakdowns', 'breakdowns__machine'
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
            total_running=Sum('total_running_minutes'),
            total_breakdown=Sum('total_breakdown_time'),
        )

        total_runs = qs.count()
        total_production = agg['total_production'] or 0
        total_running = agg['total_running'] or 0
        total_breakdown = agg['total_breakdown'] or 0

        total_time = total_running + total_breakdown
        availability = (total_running / total_time * 100) if total_time else 0

        return {
            'total_runs': total_runs,
            'total_production': total_production,
            'total_running_minutes': total_running,
            'total_breakdown_minutes': total_breakdown,
            'availability_percent': round(availability, 1),
        }
