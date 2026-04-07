import logging
from decimal import Decimal as D
from django.db import transaction
from django.utils import timezone

from ..models import (
    BOMRequest, BOMRequestLine, FinishedGoodsReceipt,
    BOMRequestStatus, BOMLineStatus, FGReceiptStatus,
    MaterialIssueStatus,
)

logger = logging.getLogger(__name__)


class WarehouseService:

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._company = None

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    # ==================================================================
    # BOM REQUEST — Creation (called from production side)
    # ==================================================================

    @transaction.atomic
    def create_bom_request(self, data: dict, user) -> BOMRequest:
        """
        Create a BOM request for a production run.
        Fetches BOM from SAP, scales quantities by required_qty.
        """
        from production_execution.models import ProductionRun

        run_id = data['production_run_id']
        required_qty = D(str(data['required_qty']))

        try:
            run = ProductionRun.objects.get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

        if run.status == 'COMPLETED':
            raise ValueError("Cannot create BOM request for a completed run.")

        # Check if there is already an active (non-rejected) BOM request
        existing = BOMRequest.objects.filter(
            production_run=run,
            status__in=[BOMRequestStatus.PENDING, BOMRequestStatus.APPROVED,
                        BOMRequestStatus.PARTIALLY_APPROVED]
        ).exists()
        if existing:
            raise ValueError("An active BOM request already exists for this run.")

        # Fetch BOM components from SAP
        components = self._fetch_bom_components(run)
        if not components:
            raise ValueError("No BOM components found for this production order.")

        # Create the BOM request
        bom_request = BOMRequest.objects.create(
            company=self.company,
            production_run=run,
            sap_doc_entry=run.sap_doc_entry,
            required_qty=required_qty,
            status=BOMRequestStatus.PENDING,
            remarks=data.get('remarks', ''),
            requested_by=user,
        )

        # Create scaled BOM lines
        for idx, comp in enumerate(components):
            per_unit = D(str(comp.get('PlannedQty', 0)))
            # For production-order BOM (WOR1), PlannedQty is already total planned
            # For item-master BOM (ITT1), PlannedQty is per-unit
            # We detect by checking if sap_doc_entry was used
            if run.sap_doc_entry:
                # WOR1: PlannedQty is for the full order, scale proportionally
                order_planned = D(str(comp.get('_order_planned_qty', 0))) or D('1')
                per_unit = per_unit / order_planned if order_planned else per_unit
                scaled_qty = per_unit * required_qty
            else:
                # ITT1: PlannedQty IS per-unit
                scaled_qty = per_unit * required_qty

            BOMRequestLine.objects.create(
                bom_request=bom_request,
                item_code=comp.get('ItemCode') or '',
                item_name=comp.get('ItemName') or '',
                per_unit_qty=per_unit,
                required_qty=scaled_qty.quantize(D('0.001')),
                warehouse=comp.get('Warehouse') or '',
                uom=comp.get('UomCode') or '',
                base_line=comp.get('LineNum') if comp.get('LineNum') is not None else idx,
                status=BOMLineStatus.PENDING,
            )

        # Update production run status
        run.warehouse_approval_status = 'PENDING'
        run.required_qty = required_qty
        run.save(update_fields=['warehouse_approval_status', 'required_qty', 'updated_at'])

        logger.info(f"BOM request #{bom_request.id} created for run #{run.id}")
        return bom_request

    def _fetch_bom_components(self, run) -> list:
        """Fetch BOM from SAP — uses production order components or item BOM."""
        from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError

        try:
            reader = ProductionOrderReader(self.company_code)

            if run.sap_doc_entry:
                detail = reader.get_production_order_detail(run.sap_doc_entry)
                header = detail.get('header', {})
                order_planned = header.get('PlannedQty', 1)
                components = detail.get('components', [])
                # Attach order planned qty for scaling
                for comp in components:
                    comp['_order_planned_qty'] = order_planned
                    comp['LineNum'] = comp.get('LineNum', 0)
                return components
            elif run.product:
                components = reader.get_bom_by_item_code(run.product)
                for idx, comp in enumerate(components):
                    comp.setdefault('IssuedQty', 0)
                    comp['LineNum'] = idx
                return components
        except SAPReadError as e:
            logger.error(f"Failed to fetch BOM for run {run.id}: {e}")
            raise ValueError(f"Failed to fetch BOM from SAP: {e}")

        return []

    # ==================================================================
    # BOM REQUEST — List & Detail (warehouse views)
    # ==================================================================

    def list_bom_requests(self, status=None, production_run_id=None):
        qs = BOMRequest.objects.filter(
            company=self.company
        ).select_related('production_run', 'production_run__line', 'requested_by', 'reviewed_by')

        if status:
            qs = qs.filter(status=status)
        if production_run_id:
            qs = qs.filter(production_run_id=production_run_id)
        return qs

    def get_bom_request(self, request_id: int) -> BOMRequest:
        try:
            return BOMRequest.objects.select_related(
                'production_run', 'production_run__line',
                'requested_by', 'reviewed_by'
            ).prefetch_related('lines').get(
                id=request_id, company=self.company
            )
        except BOMRequest.DoesNotExist:
            raise ValueError(f"BOM request {request_id} not found.")

    # ==================================================================
    # BOM REQUEST — Approval / Rejection (warehouse action)
    # ==================================================================

    @transaction.atomic
    def approve_bom_request(self, request_id: int, data: dict, user) -> BOMRequest:
        """
        Warehouse approves/partially approves BOM request.
        data.lines = [{line_id, approved_qty, status, remarks}]
        """
        bom_request = self.get_bom_request(request_id)

        if bom_request.status not in [BOMRequestStatus.PENDING]:
            raise ValueError("Only PENDING requests can be approved.")

        lines_data = data.get('lines', [])
        if not lines_data:
            raise ValueError("Approval must include line-level decisions.")

        # Fetch available stock for all lines
        stock_map = self._get_stock_for_lines(bom_request)

        all_approved = True
        any_approved = False

        for line_data in lines_data:
            try:
                line = BOMRequestLine.objects.get(
                    id=line_data['line_id'],
                    bom_request=bom_request
                )
            except BOMRequestLine.DoesNotExist:
                raise ValueError(f"BOM line {line_data['line_id']} not found.")

            line_status = line_data.get('status', 'APPROVED')
            approved_qty = D(str(line_data.get('approved_qty', line.required_qty)))

            # Update available stock
            line.available_stock = D(str(stock_map.get(line.item_code, {}).get('OnHand', 0)))

            if line_status == 'APPROVED':
                line.approved_qty = approved_qty
                line.status = BOMLineStatus.APPROVED
                any_approved = True
            elif line_status == 'REJECTED':
                line.approved_qty = 0
                line.status = BOMLineStatus.REJECTED
                all_approved = False
            else:
                all_approved = False

            line.remarks = line_data.get('remarks', line.remarks)
            line.save()

        # Determine overall status
        if all_approved and any_approved:
            bom_request.status = BOMRequestStatus.APPROVED
        elif any_approved:
            bom_request.status = BOMRequestStatus.PARTIALLY_APPROVED
        else:
            bom_request.status = BOMRequestStatus.REJECTED

        bom_request.reviewed_by = user
        bom_request.reviewed_at = timezone.now()
        bom_request.save()

        # Update production run warehouse approval status
        run = bom_request.production_run
        run.warehouse_approval_status = bom_request.status
        run.save(update_fields=['warehouse_approval_status', 'updated_at'])

        logger.info(f"BOM request #{request_id} → {bom_request.status} by {user}")
        return bom_request

    @transaction.atomic
    def reject_bom_request(self, request_id: int, reason: str, user) -> BOMRequest:
        """Warehouse rejects entire BOM request."""
        bom_request = self.get_bom_request(request_id)

        if bom_request.status not in [BOMRequestStatus.PENDING]:
            raise ValueError("Only PENDING requests can be rejected.")

        bom_request.status = BOMRequestStatus.REJECTED
        bom_request.rejection_reason = reason
        bom_request.reviewed_by = user
        bom_request.reviewed_at = timezone.now()
        bom_request.save()

        # Update all lines
        bom_request.lines.update(status=BOMLineStatus.REJECTED)

        # Update production run
        run = bom_request.production_run
        run.warehouse_approval_status = 'REJECTED'
        run.save(update_fields=['warehouse_approval_status', 'updated_at'])

        logger.info(f"BOM request #{request_id} REJECTED by {user}: {reason}")
        return bom_request

    # ==================================================================
    # STOCK CHECK — Query SAP OITW for warehouse stock
    # ==================================================================

    def _get_stock_for_lines(self, bom_request: BOMRequest) -> dict:
        """Fetch stock from SAP OITW for all items in the BOM request."""
        from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError

        try:
            reader = ProductionOrderReader(self.company_code)
            schema = reader.client.context.config['hana']['schema']

            item_codes = list(bom_request.lines.values_list('item_code', flat=True))
            if not item_codes:
                return {}

            codes_str = ', '.join(f"'{c}'" for c in item_codes)
            sql = """
                SELECT
                    T0."ItemCode",
                    T0."ItemName",
                    T1."WhsCode",
                    T1."OnHand",
                    T1."IsCommited",
                    T1."OnOrder",
                    (T1."OnHand" - T1."IsCommited") AS "Available"
                FROM "{schema}"."OITM" T0
                INNER JOIN "{schema}"."OITW" T1 ON T0."ItemCode" = T1."ItemCode"
                WHERE T0."ItemCode" IN ({codes})
                  AND T1."OnHand" > 0
            """.format(schema=schema, codes=codes_str)

            rows = reader._execute(sql)
            # Group by ItemCode, sum across warehouses
            stock_map = {}
            for row in rows:
                code = row['ItemCode']
                if code not in stock_map:
                    stock_map[code] = {'OnHand': 0, 'Available': 0, 'warehouses': []}
                stock_map[code]['OnHand'] += float(row.get('OnHand', 0))
                stock_map[code]['Available'] += float(row.get('Available', 0))
                stock_map[code]['warehouses'].append({
                    'WhsCode': row.get('WhsCode', ''),
                    'OnHand': float(row.get('OnHand', 0)),
                    'Available': float(row.get('Available', 0)),
                })
            return stock_map

        except Exception as e:
            logger.error(f"Failed to fetch stock: {e}")
            return {}

    def get_stock_for_items(self, item_codes: list) -> dict:
        """Public method to check stock for a list of item codes."""
        from production_execution.services.sap_reader import ProductionOrderReader

        try:
            reader = ProductionOrderReader(self.company_code)
            schema = reader.client.context.config['hana']['schema']

            if not item_codes:
                return {}

            codes_str = ', '.join(f"'{c}'" for c in item_codes)
            sql = """
                SELECT
                    T0."ItemCode",
                    T0."ItemName",
                    T1."WhsCode",
                    T1."OnHand",
                    T1."IsCommited",
                    (T1."OnHand" - T1."IsCommited") AS "Available"
                FROM "{schema}"."OITM" T0
                INNER JOIN "{schema}"."OITW" T1 ON T0."ItemCode" = T1."ItemCode"
                WHERE T0."ItemCode" IN ({codes})
            """.format(schema=schema, codes=codes_str)

            rows = reader._execute(sql)
            result = {}
            for row in rows:
                code = row['ItemCode']
                if code not in result:
                    result[code] = {
                        'ItemCode': code,
                        'ItemName': row.get('ItemName', ''),
                        'total_on_hand': 0,
                        'total_available': 0,
                        'warehouses': []
                    }
                result[code]['total_on_hand'] += float(row.get('OnHand', 0))
                result[code]['total_available'] += float(row.get('Available', 0))
                result[code]['warehouses'].append({
                    'WhsCode': row.get('WhsCode', ''),
                    'OnHand': float(row.get('OnHand', 0)),
                    'Available': float(row.get('Available', 0)),
                })
            return result
        except Exception as e:
            logger.error(f"Stock check failed: {e}")
            return {}

    # ==================================================================
    # SAP HELPERS
    # ==================================================================

    def _get_branch_for_warehouse(self, warehouse_code: str) -> int | None:
        """Look up BPLid from OWHS for the given warehouse."""
        from production_execution.services.sap_reader import ProductionOrderReader
        try:
            reader = ProductionOrderReader(self.company_code)
            schema = reader.client.context.config['hana']['schema']
            sql = """
                SELECT "BPLid" FROM "{schema}"."OWHS"
                WHERE "WhsCode" = '{wh}'
            """.format(schema=schema, wh=warehouse_code.replace("'", "''"))
            rows = reader._execute(sql)
            if rows:
                return rows[0].get('BPLid')
        except Exception as e:
            logger.warning(f"Could not look up branch for warehouse {warehouse_code}: {e}")
        return None

    def _get_branch_for_order(self, sap_doc_entry: int) -> int | None:
        """Look up BPLid from the production order's warehouse."""
        from production_execution.services.sap_reader import ProductionOrderReader
        try:
            reader = ProductionOrderReader(self.company_code)
            detail = reader.get_production_order_detail(sap_doc_entry)
            wh = detail.get('header', {}).get('Warehouse', '')
            if wh:
                return self._get_branch_for_warehouse(wh)
        except Exception as e:
            logger.warning(f"Could not look up branch for order {sap_doc_entry}: {e}")
        return None

    # ==================================================================
    # MATERIAL ISSUE — Post goods issue to SAP (after approval)
    # ==================================================================

    @transaction.atomic
    def issue_materials_to_sap(self, request_id: int, data: dict, user) -> BOMRequest:
        """
        Issue approved materials to SAP via InventoryGenExits.
        data.lines = [{line_id, quantity}]  (optional, defaults to approved_qty)
        """
        import requests as http_requests

        bom_request = self.get_bom_request(request_id)

        if bom_request.status not in [BOMRequestStatus.APPROVED, BOMRequestStatus.PARTIALLY_APPROVED]:
            raise ValueError("Can only issue materials for approved BOM requests.")

        # Get lines to issue
        lines_to_issue = []
        lines_data = data.get('lines', [])

        if lines_data:
            # Specific lines provided
            for ld in lines_data:
                line = BOMRequestLine.objects.get(id=ld['line_id'], bom_request=bom_request)
                if line.status != BOMLineStatus.APPROVED:
                    raise ValueError(f"Line {line.item_code} is not approved.")
                qty = D(str(ld.get('quantity', line.approved_qty)))
                remaining = line.approved_qty - line.issued_qty
                if qty > remaining:
                    raise ValueError(
                        f"Issue qty {qty} exceeds remaining approved qty {remaining} "
                        f"for {line.item_code}"
                    )
                lines_to_issue.append({'line': line, 'qty': qty})
        else:
            # Issue all approved lines for their remaining qty
            for line in bom_request.lines.filter(status=BOMLineStatus.APPROVED):
                remaining = line.approved_qty - line.issued_qty
                if remaining > 0:
                    lines_to_issue.append({'line': line, 'qty': remaining})

        if not lines_to_issue:
            raise ValueError("No materials to issue.")

        # Build SAP payload
        from sap_client.client import SAPClient
        client = SAPClient(company_code=self.company_code)
        sl_config = client.context.service_layer
        base_url = sl_config['base_url']

        session = http_requests.Session()
        login_resp = session.post(
            f"{base_url}/b1s/v2/Login",
            json={
                "CompanyDB": sl_config['company_db'],
                "UserName": sl_config['username'],
                "Password": sl_config['password'],
            },
            timeout=10,
            verify=False,
        )
        if not login_resp.ok:
            raise ValueError(f"SAP login failed: {login_resp.text}")

        posting_date = data.get('posting_date', timezone.now().date().isoformat())
        doc_entry = bom_request.sap_doc_entry

        document_lines = []
        for item in lines_to_issue:
            line = item['line']
            # When BaseType=202 (production order), SAP derives ItemCode
            # and WarehouseCode from the base document — do NOT send them.
            document_lines.append({
                "Quantity": float(item['qty']),
                "BaseType": 202,
                "BaseEntry": doc_entry,
                "BaseLine": line.base_line,
            })

        payload = {
            "DocDate": posting_date,
            "Comments": f"Issue for Production — BOM Request #{bom_request.id}, Run #{bom_request.production_run_id}",
            "DocumentLines": document_lines,
        }

        # Add branch ID if multi-branch is enabled
        branch_id = self._get_branch_for_order(doc_entry)
        if branch_id is not None:
            payload["BPL_IDAssignedToInvoice"] = branch_id

        response = session.post(
            f"{base_url}/b1s/v2/InventoryGenExits",
            json=payload,
            timeout=30,
            verify=False,
        )

        if not response.ok:
            try:
                err = response.json().get('error', {}).get('message', {}).get('value', response.text)
            except Exception:
                err = response.text
            raise ValueError(f"SAP issue failed: {err}")

        result = response.json()
        sap_doc_entry_created = result.get('DocEntry')
        sap_doc_num = result.get('DocNum')

        # Update issued quantities
        for item in lines_to_issue:
            line = item['line']
            line.issued_qty += item['qty']
            line.save(update_fields=['issued_qty', 'updated_at'])

        # Track SAP document
        entries = bom_request.sap_issue_doc_entries or []
        entries.append({
            'doc_entry': sap_doc_entry_created,
            'doc_num': sap_doc_num,
            'date': posting_date,
            'lines_count': len(lines_to_issue),
        })
        bom_request.sap_issue_doc_entries = entries

        # Determine issue status
        all_fully_issued = all(
            line.issued_qty >= line.approved_qty
            for line in bom_request.lines.filter(status=BOMLineStatus.APPROVED)
        )
        bom_request.material_issue_status = (
            MaterialIssueStatus.FULLY_ISSUED if all_fully_issued
            else MaterialIssueStatus.PARTIALLY_ISSUED
        )
        bom_request.save()

        logger.info(
            f"Materials issued to SAP for BOM request #{request_id}. "
            f"SAP DocEntry={sap_doc_entry_created}"
        )
        return bom_request

    # ==================================================================
    # FINISHED GOODS RECEIPT — Post-production warehouse receives FG
    # ==================================================================

    @transaction.atomic
    def create_fg_receipt(self, data: dict, user) -> FinishedGoodsReceipt:
        """
        Create a finished goods receipt record when production is completed.
        Called automatically or manually by warehouse.
        """
        from production_execution.models import ProductionRun

        run_id = data['production_run_id']
        try:
            run = ProductionRun.objects.get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

        if run.status != 'COMPLETED':
            raise ValueError("Can only create FG receipt for completed runs.")

        # Check if already received
        existing = FinishedGoodsReceipt.objects.filter(
            production_run=run,
            status__in=[FGReceiptStatus.RECEIVED, FGReceiptStatus.SAP_POSTED]
        ).exists()
        if existing:
            raise ValueError("Finished goods already received for this run.")

        produced_qty = run.total_production
        rejected_qty = run.rejected_qty
        good_qty = produced_qty - rejected_qty

        # Fetch item details from SAP if linked
        item_code = data.get('item_code', '')
        item_name = data.get('item_name', '')
        warehouse = data.get('warehouse', '')

        if run.sap_doc_entry and not item_code:
            from production_execution.services.sap_reader import ProductionOrderReader
            try:
                reader = ProductionOrderReader(self.company_code)
                detail = reader.get_production_order_detail(run.sap_doc_entry)
                header = detail.get('header', {})
                item_code = header.get('ItemCode', '')
                item_name = header.get('ProdName', '')
                warehouse = header.get('Warehouse', '')
            except Exception as e:
                logger.warning(f"Could not fetch SAP order detail for FG receipt: {e}")

        receipt = FinishedGoodsReceipt.objects.create(
            company=self.company,
            production_run=run,
            sap_doc_entry=run.sap_doc_entry,
            item_code=item_code,
            item_name=item_name,
            produced_qty=produced_qty,
            good_qty=good_qty,
            rejected_qty=rejected_qty,
            warehouse=warehouse,
            uom=data.get('uom', ''),
            posting_date=data.get('posting_date', run.date),
            status=FGReceiptStatus.PENDING,
        )

        logger.info(f"FG receipt #{receipt.id} created for run #{run.id}")
        return receipt

    @transaction.atomic
    def receive_finished_goods(self, receipt_id: int, user) -> FinishedGoodsReceipt:
        """Warehouse confirms receipt of finished goods."""
        try:
            receipt = FinishedGoodsReceipt.objects.get(
                id=receipt_id, company=self.company
            )
        except FinishedGoodsReceipt.DoesNotExist:
            raise ValueError(f"FG receipt {receipt_id} not found.")

        if receipt.status not in [FGReceiptStatus.PENDING, FGReceiptStatus.FAILED]:
            raise ValueError("Receipt is already processed.")

        receipt.status = FGReceiptStatus.RECEIVED
        receipt.received_by = user
        receipt.received_at = timezone.now()
        receipt.save()

        logger.info(f"FG receipt #{receipt_id} received by {user}")
        return receipt

    @transaction.atomic
    def post_fg_receipt_to_sap(self, receipt_id: int) -> FinishedGoodsReceipt:
        """Post finished goods receipt to SAP InventoryGenEntries."""
        import requests as http_requests

        try:
            receipt = FinishedGoodsReceipt.objects.get(
                id=receipt_id, company=self.company
            )
        except FinishedGoodsReceipt.DoesNotExist:
            raise ValueError(f"FG receipt {receipt_id} not found.")

        if receipt.status not in [FGReceiptStatus.RECEIVED, FGReceiptStatus.FAILED]:
            raise ValueError("Receipt must be in RECEIVED or FAILED status to post to SAP.")

        if not receipt.sap_doc_entry:
            raise ValueError("No SAP production order linked.")

        if not receipt.item_code or not receipt.warehouse:
            raise ValueError("Missing item code or warehouse for SAP posting.")

        qty = float(receipt.good_qty)
        if qty <= 0:
            raise ValueError("Good quantity must be > 0 to post to SAP.")

        from sap_client.client import SAPClient
        client = SAPClient(company_code=self.company_code)
        sl_config = client.context.service_layer
        base_url = sl_config['base_url']

        session = http_requests.Session()
        login_resp = session.post(
            f"{base_url}/b1s/v2/Login",
            json={
                "CompanyDB": sl_config['company_db'],
                "UserName": sl_config['username'],
                "Password": sl_config['password'],
            },
            timeout=10,
            verify=False,
        )
        if not login_resp.ok:
            receipt.status = FGReceiptStatus.FAILED
            receipt.sap_error = f"SAP login failed: {login_resp.text}"
            receipt.save()
            raise ValueError(receipt.sap_error)

        payload = {
            "DocDate": receipt.posting_date.isoformat(),
            "Comments": f"FG Receipt — Run #{receipt.production_run_id}, Receipt #{receipt.id}",
            # When BaseType=202, SAP derives ItemCode/Warehouse from the order
            "DocumentLines": [{
                "Quantity": qty,
                "BaseType": 202,
                "BaseEntry": receipt.sap_doc_entry,
                "BaseLine": 0,
            }],
        }

        # Add branch ID if multi-branch is enabled
        branch_id = self._get_branch_for_order(receipt.sap_doc_entry)
        if branch_id is not None:
            payload["BPL_IDAssignedToInvoice"] = branch_id

        response = session.post(
            f"{base_url}/b1s/v2/InventoryGenEntries",
            json=payload,
            timeout=30,
            verify=False,
        )

        if not response.ok:
            try:
                err = response.json().get('error', {}).get('message', {}).get('value', response.text)
            except Exception:
                err = response.text
            receipt.status = FGReceiptStatus.FAILED
            receipt.sap_error = err
            receipt.save()
            raise ValueError(f"SAP posting failed: {err}")

        result = response.json()
        receipt.sap_receipt_doc_entry = result.get('DocEntry')
        receipt.status = FGReceiptStatus.SAP_POSTED
        receipt.sap_error = ''
        receipt.save()

        # Also update the production run SAP fields
        run = receipt.production_run
        run.sap_receipt_doc_entry = receipt.sap_receipt_doc_entry
        run.sap_sync_status = 'SUCCESS'
        run.sap_sync_error = ''
        run.save(update_fields=[
            'sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error', 'updated_at'
        ])

        logger.info(
            f"FG receipt #{receipt_id} posted to SAP. "
            f"DocEntry={receipt.sap_receipt_doc_entry}"
        )
        return receipt

    # ==================================================================
    # FINISHED GOODS — List & Detail
    # ==================================================================

    def list_fg_receipts(self, status=None, production_run_id=None):
        qs = FinishedGoodsReceipt.objects.filter(
            company=self.company
        ).select_related('production_run', 'received_by')

        if status:
            qs = qs.filter(status=status)
        if production_run_id:
            qs = qs.filter(production_run_id=production_run_id)
        return qs

    def get_fg_receipt(self, receipt_id: int) -> FinishedGoodsReceipt:
        try:
            return FinishedGoodsReceipt.objects.select_related(
                'production_run', 'received_by'
            ).get(id=receipt_id, company=self.company)
        except FinishedGoodsReceipt.DoesNotExist:
            raise ValueError(f"FG receipt {receipt_id} not found.")
