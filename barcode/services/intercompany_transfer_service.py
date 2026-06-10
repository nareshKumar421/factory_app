from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from company.models import Company, UserCompany

from ..models import (
    BarcodeAuditLog,
    BarcodeAuditTransactionType,
    BarcodeMaster,
    Box,
    BoxStatus,
    EntityType,
    IntercompanyTransfer,
    IntercompanyTransferLine,
    IntercompanyTransferStatus,
    Pallet,
    PalletStatus,
)
from .scan_service import ScanService
from .oitm_item_service import OitmItemReadError, OitmItemService


class IntercompanyTransferError(ValueError):
    pass


class IntercompanyTransferService:
    JIVO_OIL_COMPANY_CODE = "JIVO_OIL"
    JIVO_MART_COMPANY_CODE = "JIVO_MART"

    def __init__(self, user):
        self.user = user

    def _company(self, code: str) -> Company:
        try:
            return Company.objects.get(code=code, is_active=True)
        except Company.DoesNotExist as exc:
            raise IntercompanyTransferError(f"Company {code} is not active or does not exist.") from exc

    def _require_user_company(self, company: Company) -> None:
        if not UserCompany.objects.filter(
            user=self.user,
            company=company,
            is_active=True,
        ).exists():
            raise IntercompanyTransferError(f"You do not have access to {company.code}.")

    def _validate_company_pair(self, source_code: str, destination_code: str) -> tuple[Company, Company]:
        source = self._company(source_code)
        destination = self._company(destination_code)
        if source.id == destination.id:
            raise IntercompanyTransferError("Source and destination company cannot be the same.")
        self._require_user_company(source)
        self._require_user_company(destination)
        return source, destination

    @staticmethod
    def _transfer_number() -> str:
        stamp = timezone.localtime().strftime("%Y%m%d%H%M%S%f")
        return f"ICBT-{stamp}"

    @staticmethod
    def _box_payload(box: Box) -> dict:
        return {
            "id": box.id,
            "entity_type": EntityType.BOX,
            "barcode": box.box_barcode,
            "item_code": box.item_code,
            "item_name": box.item_name,
            "batch_number": box.batch_number,
            "qty": str(box.qty),
            "uom": box.uom,
            "current_company": box.company.code,
            "current_company_name": box.company.name,
            "current_warehouse": box.current_warehouse,
            "status": box.status,
            "dispatch_status": "DISPATCHED" if box.dispatched_at else "NOT_DISPATCHED",
        }

    @staticmethod
    def _pallet_payload(pallet: Pallet, boxes: list[Box]) -> dict:
        return {
            "id": pallet.id,
            "entity_type": EntityType.PALLET,
            "barcode": pallet.pallet_id,
            "item_code": pallet.item_code,
            "item_name": pallet.item_name,
            "batch_number": pallet.batch_number,
            "qty": str(sum((box.qty for box in boxes), Decimal("0"))),
            "uom": pallet.uom,
            "box_count": len(boxes),
            "current_company": pallet.company.code,
            "current_company_name": pallet.company.name,
            "current_warehouse": pallet.current_warehouse,
            "status": pallet.status,
            "dispatch_status": "DISPATCHED" if pallet.dispatched_at else "NOT_DISPATCHED",
        }

    @staticmethod
    def _canonical_box_lookup(raw_barcode: str, source: Company) -> Box | None:
        lookup = ScanService(source.code).lookup_barcode(raw_barcode)
        if lookup.get("entity_type") == "PALLET":
            raise IntercompanyTransferError("Scanned barcode is a pallet. Select Pallet transfer type.")
        if lookup.get("entity_type") == "BOX" and lookup.get("entity_id"):
            try:
                return Box.objects.select_related("company").get(id=lookup["entity_id"])
            except Box.DoesNotExist:
                return None

        # Last-chance exact global lookup. This handles boxes that may already
        # have moved ownership but are still being traced by their canonical ID.
        return Box.objects.select_related("company").filter(box_barcode=raw_barcode).first()

    @staticmethod
    def _canonical_pallet_lookup(raw_barcode: str, source: Company) -> Pallet | None:
        lookup = ScanService(source.code).lookup_barcode(raw_barcode)
        if lookup.get("entity_type") == "BOX":
            raise IntercompanyTransferError("Scanned barcode is a box. Select Box transfer type.")
        if lookup.get("entity_type") == "PALLET" and lookup.get("entity_id"):
            try:
                return Pallet.objects.select_related("company").get(id=lookup["entity_id"])
            except Pallet.DoesNotExist:
                return None

        return Pallet.objects.select_related("company").filter(pallet_id=raw_barcode).first()

    def _get_box_for_scan(self, barcode: str, source: Company) -> Box:
        barcode = str(barcode or "").strip()
        if not barcode:
            raise IntercompanyTransferError("Barcode is required.")
        box = self._canonical_box_lookup(barcode, source)
        if not box:
            raise IntercompanyTransferError("Barcode does not exist.")
        return box

    def _get_pallet_for_scan(self, barcode: str, source: Company) -> Pallet:
        barcode = str(barcode or "").strip()
        if not barcode:
            raise IntercompanyTransferError("Barcode is required.")
        pallet = self._canonical_pallet_lookup(barcode, source)
        if not pallet:
            raise IntercompanyTransferError("Pallet barcode does not exist.")
        return pallet

    @staticmethod
    def _normalize_transfer_type(transfer_type: str | None) -> str:
        value = str(transfer_type or EntityType.BOX).strip().upper()
        if value not in (EntityType.BOX, EntityType.PALLET):
            raise IntercompanyTransferError("Transfer type must be BOX or PALLET.")
        return value

    @classmethod
    def _requires_jivo_mart_item_mapping(cls, source: Company, destination: Company) -> bool:
        return (
            str(source.code or "").upper() == cls.JIVO_OIL_COMPANY_CODE
            and str(destination.code or "").upper() == cls.JIVO_MART_COMPANY_CODE
        )

    def _destination_item_code_map(self, source: Company, destination: Company, boxes: list[Box]) -> dict[str, str]:
        if not self._requires_jivo_mart_item_mapping(source, destination):
            return {}

        oil_item_codes = sorted({str(box.item_code or "").strip() for box in boxes})
        mapper = OitmItemService(company_code=destination.code)
        destination_codes: dict[str, str] = {}
        for oil_item_code in oil_item_codes:
            try:
                matches = mapper.find_item_codes_by_oil_item_code(oil_item_code)
            except OitmItemReadError as exc:
                raise IntercompanyTransferError(str(exc)) from exc

            if not matches:
                raise IntercompanyTransferError(
                    "Item mapping not found in Jivo Mart for Oil ItemCode: "
                    f"{oil_item_code}. Please maintain U_Oil_ItemCode in Jivo Mart OITM table."
                )
            if len(matches) > 1:
                raise IntercompanyTransferError(
                    "Duplicate item mapping found in Jivo Mart for Oil ItemCode: "
                    f"{oil_item_code}. Please correct duplicate U_Oil_ItemCode values in Jivo Mart OITM table."
                )
            destination_codes[oil_item_code] = matches[0]
        return destination_codes

    @staticmethod
    def _move_boxes_to_destination(
        *,
        boxes: list[Box],
        destination: Company,
        destination_item_code_by_oil_code: dict[str, str],
    ) -> None:
        if not destination_item_code_by_oil_code:
            Box.objects.filter(id__in=[box.id for box in boxes]).update(company=destination)
            BarcodeMaster.objects.filter(box_id__in=[box.id for box in boxes]).update(company=destination)
            return

        now = timezone.now()
        for box in boxes:
            oil_item_code = str(box.item_code or "").strip()
            box.company = destination
            box.item_code = destination_item_code_by_oil_code[oil_item_code]
            box.updated_at = now
        Box.objects.bulk_update(boxes, ["company", "item_code", "updated_at"])

        for destination_item_code in destination_item_code_by_oil_code.values():
            box_ids = [box.id for box in boxes if box.item_code == destination_item_code]
            BarcodeMaster.objects.filter(box_id__in=box_ids).update(
                company=destination,
                material_code=destination_item_code,
            )

    @staticmethod
    def _move_pallets_to_destination(
        *,
        pallets: list[Pallet],
        destination: Company,
        destination_item_code_by_oil_code: dict[str, str],
    ) -> None:
        if not pallets:
            return
        if not destination_item_code_by_oil_code:
            Pallet.objects.filter(id__in=[pallet.id for pallet in pallets]).update(company=destination)
            BarcodeMaster.objects.filter(pallet_id__in=[pallet.id for pallet in pallets]).update(
                company=destination
            )
            return

        now = timezone.now()
        for pallet in pallets:
            oil_item_code = str(pallet.item_code or "").strip()
            pallet.company = destination
            if oil_item_code in destination_item_code_by_oil_code:
                pallet.item_code = destination_item_code_by_oil_code[oil_item_code]
            pallet.updated_at = now
        Pallet.objects.bulk_update(pallets, ["company", "item_code", "updated_at"])

        for destination_item_code in destination_item_code_by_oil_code.values():
            pallet_ids = [pallet.id for pallet in pallets if pallet.item_code == destination_item_code]
            BarcodeMaster.objects.filter(pallet_id__in=pallet_ids).update(
                company=destination,
                material_code=destination_item_code,
            )

    @staticmethod
    def _restore_boxes_to_source(
        *,
        boxes: list[Box],
        source: Company,
        oil_item_code_by_box_id: dict[int, str],
    ) -> None:
        if not oil_item_code_by_box_id:
            Box.objects.filter(id__in=[box.id for box in boxes]).update(company=source)
            BarcodeMaster.objects.filter(box_id__in=[box.id for box in boxes]).update(company=source)
            return

        now = timezone.now()
        for box in boxes:
            box.company = source
            box.item_code = oil_item_code_by_box_id.get(box.id, box.item_code)
            box.updated_at = now
        Box.objects.bulk_update(boxes, ["company", "item_code", "updated_at"])

        for box in boxes:
            BarcodeMaster.objects.filter(box_id=box.id).update(
                company=source,
                material_code=box.item_code,
            )

    @staticmethod
    def _restore_pallets_to_source(
        *,
        pallets: list[Pallet],
        source: Company,
        oil_item_code_by_pallet_id: dict[int, str],
    ) -> None:
        if not pallets:
            return
        if not oil_item_code_by_pallet_id:
            Pallet.objects.filter(id__in=[pallet.id for pallet in pallets]).update(company=source)
            BarcodeMaster.objects.filter(pallet_id__in=[pallet.id for pallet in pallets]).update(
                company=source
            )
            return

        now = timezone.now()
        for pallet in pallets:
            pallet.company = source
            pallet.item_code = oil_item_code_by_pallet_id.get(pallet.id, pallet.item_code)
            pallet.updated_at = now
        Pallet.objects.bulk_update(pallets, ["company", "item_code", "updated_at"])

        for pallet in pallets:
            BarcodeMaster.objects.filter(pallet_id=pallet.id).update(
                company=source,
                material_code=pallet.item_code,
            )

    def _active_pallet_boxes(self, pallet: Pallet) -> list[Box]:
        return list(
            pallet.boxes
            .select_related("company", "pallet")
            .filter(status__in=(BoxStatus.ACTIVE, BoxStatus.PARTIAL), dispatched_at__isnull=True)
            .order_by("id")
        )

    def _validate_box(self, box: Box, source: Company, label: str) -> None:
        if box.company_id != source.id:
            raise IntercompanyTransferError(f"{label} does not belong to {source.code}.")
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            raise IntercompanyTransferError(f"{label} is not active.")
        if box.dispatched_at or box.status == BoxStatus.DISPATCHED:
            raise IntercompanyTransferError(f"{label} is already dispatched.")

    def _validate_pallet(self, pallet: Pallet, source: Company) -> list[Box]:
        if pallet.company_id != source.id:
            raise IntercompanyTransferError(f"{pallet.pallet_id} does not belong to {source.code}.")
        if pallet.status not in (PalletStatus.ACTIVE, PalletStatus.PARTIAL):
            raise IntercompanyTransferError(f"{pallet.pallet_id} is not active.")
        if pallet.dispatched_at or pallet.status == PalletStatus.DISPATCHED:
            raise IntercompanyTransferError(f"{pallet.pallet_id} is already dispatched.")

        boxes = self._active_pallet_boxes(pallet)
        if not boxes:
            raise IntercompanyTransferError(f"{pallet.pallet_id} has no active boxes to transfer.")
        for box in boxes:
            self._validate_box(box, source, box.box_barcode)
        return boxes

    def scan_barcode(
        self,
        *,
        barcode: str,
        source_company_code: str,
        destination_company_code: str,
        transfer_type: str = EntityType.BOX,
        device_id: str = "",
    ) -> dict:
        source, destination = self._validate_company_pair(source_company_code, destination_company_code)
        transfer_type = self._normalize_transfer_type(transfer_type)

        if transfer_type == EntityType.PALLET:
            pallet = self._get_pallet_for_scan(barcode, source)
            boxes = self._validate_pallet(pallet, source)

            BarcodeAuditLog.objects.bulk_create([
                BarcodeAuditLog(
                    box=box,
                    barcode=box.box_barcode,
                    transaction_type=BarcodeAuditTransactionType.SCANNED,
                    from_company=source,
                    to_company=destination,
                    user=self.user,
                    device_id=device_id,
                    notes=f"Intercompany pallet scan validation: {pallet.pallet_id}",
                )
                for box in boxes
            ])
            return self._pallet_payload(pallet, boxes)

        box = self._get_box_for_scan(barcode, source)
        BarcodeAuditLog.objects.create(
            box=box,
            barcode=box.box_barcode,
            transaction_type=BarcodeAuditTransactionType.SCANNED,
            from_company=source,
            to_company=destination,
            user=self.user,
            device_id=device_id,
            notes="Intercompany transfer scan validation",
        )

        self._validate_box(box, source, "Barcode")
        return self._box_payload(box)

    @transaction.atomic
    def create_transfer(
        self,
        *,
        source_company_code: str,
        destination_company_code: str,
        barcodes: list[str],
        transfer_type: str = EntityType.BOX,
        notes: str = "",
        device_id: str = "",
        sap_enabled: bool = False,
    ) -> IntercompanyTransfer:
        source, destination = self._validate_company_pair(source_company_code, destination_company_code)
        transfer_type = self._normalize_transfer_type(transfer_type)
        clean_barcodes = []
        seen = set()
        for barcode in barcodes:
            value = str(barcode or "").strip()
            if value and value not in seen:
                seen.add(value)
                clean_barcodes.append(value)
        if not clean_barcodes:
            raise IntercompanyTransferError("Scan at least one barcode before confirming transfer.")

        boxes = []
        pallets = []
        box_ids = set()
        for barcode in clean_barcodes:
            if transfer_type == EntityType.PALLET:
                pallet = self._get_pallet_for_scan(barcode, source)
                if pallet.id in {existing.id for existing in pallets}:
                    continue
                pallet_boxes = self._validate_pallet(pallet, source)
                pallets.append(pallet)
                for box in pallet_boxes:
                    if box.id not in box_ids:
                        box_ids.add(box.id)
                        boxes.append(box)
            else:
                box = self._get_box_for_scan(barcode, source)
                if box.id in box_ids:
                    continue
                self._validate_box(box, source, barcode)
                box_ids.add(box.id)
                boxes.append(box)

        destination_item_code_by_oil_code = self._destination_item_code_map(
            source,
            destination,
            boxes,
        )
        boxes = list(
            Box.objects
            .select_for_update()
            .select_related("company")
            .filter(id__in=[box.id for box in boxes])
        )
        pallets = list(
            Pallet.objects
            .select_for_update()
            .filter(id__in=[pallet.id for pallet in pallets])
        )

        total_qty = sum((box.qty for box in boxes), Decimal("0"))
        uoms = {box.uom for box in boxes if box.uom}
        transfer = IntercompanyTransfer.objects.create(
            transfer_number=self._transfer_number(),
            entity_type=transfer_type,
            source_company=source,
            destination_company=destination,
            total_barcodes=len(boxes),
            total_qty=total_qty,
            uom=uoms.pop() if len(uoms) == 1 else "",
            notes=notes,
            device_id=device_id,
            sap_enabled=sap_enabled,
            sap_status="PENDING" if sap_enabled else "",
            created_by=self.user,
        )

        lines = [
            IntercompanyTransferLine(
                transfer=transfer,
                box=box,
                barcode=box.box_barcode,
                item_code=box.item_code,
                item_name=box.item_name,
                batch_number=box.batch_number,
                qty=box.qty,
                uom=box.uom,
                from_company=source,
                to_company=destination,
            )
            for box in boxes
        ]
        IntercompanyTransferLine.objects.bulk_create(lines)

        self._move_boxes_to_destination(
            boxes=boxes,
            destination=destination,
            destination_item_code_by_oil_code=destination_item_code_by_oil_code,
        )
        if transfer_type == EntityType.PALLET:
            self._move_pallets_to_destination(
                pallets=pallets,
                destination=destination,
                destination_item_code_by_oil_code=destination_item_code_by_oil_code,
            )

        BarcodeAuditLog.objects.bulk_create([
            BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.TRANSFER_COMPLETED,
                transfer=transfer,
                from_company=source,
                to_company=destination,
                user=self.user,
                device_id=device_id,
                notes=f"Transferred via {transfer.transfer_number}",
            )
            for box in boxes
        ])
        return transfer

    @transaction.atomic
    def reverse_transfer(self, transfer_id: int, *, reason: str = "", device_id: str = "") -> IntercompanyTransfer:
        transfer = (
            IntercompanyTransfer.objects
            .select_for_update()
            .select_related("source_company", "destination_company")
            .prefetch_related("lines__box")
            .get(id=transfer_id)
        )
        self._require_user_company(transfer.source_company)
        self._require_user_company(transfer.destination_company)

        if transfer.status == IntercompanyTransferStatus.REVERSED:
            raise IntercompanyTransferError("Transfer is already reversed.")

        lines = list(transfer.lines.all())
        boxes = [line.box for line in lines]
        for box in boxes:
            if box.company_id != transfer.destination_company_id:
                raise IntercompanyTransferError(
                    f"{box.box_barcode} is no longer owned by {transfer.destination_company.code}."
                )
            if box.dispatched_at or box.status == BoxStatus.DISPATCHED:
                raise IntercompanyTransferError(f"{box.box_barcode} is already dispatched and cannot be reversed.")

        is_mapped_route = self._requires_jivo_mart_item_mapping(
            transfer.source_company,
            transfer.destination_company,
        )
        oil_item_code_by_box_id = (
            {line.box_id: line.item_code for line in lines}
            if is_mapped_route
            else {}
        )
        self._restore_boxes_to_source(
            boxes=boxes,
            source=transfer.source_company,
            oil_item_code_by_box_id=oil_item_code_by_box_id,
        )
        if transfer.entity_type == EntityType.PALLET:
            pallet_ids = {box.pallet_id for box in boxes if box.pallet_id}
            pallets = list(Pallet.objects.select_for_update().filter(id__in=pallet_ids))
            oil_item_code_by_pallet_id = {}
            if is_mapped_route:
                for line in lines:
                    if line.box.pallet_id:
                        oil_item_code_by_pallet_id.setdefault(line.box.pallet_id, line.item_code)
            self._restore_pallets_to_source(
                pallets=pallets,
                source=transfer.source_company,
                oil_item_code_by_pallet_id=oil_item_code_by_pallet_id,
            )
        transfer.status = IntercompanyTransferStatus.REVERSED
        transfer.reversed_at = timezone.now()
        transfer.reversed_by = self.user
        transfer.notes = "\n".join(part for part in [transfer.notes, f"Reverse reason: {reason}"] if part)
        transfer.save(update_fields=["status", "reversed_at", "reversed_by", "notes", "updated_at"])

        BarcodeAuditLog.objects.bulk_create([
            BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.TRANSFER_REVERSED,
                transfer=transfer,
                from_company=transfer.destination_company,
                to_company=transfer.source_company,
                user=self.user,
                device_id=device_id,
                notes=reason,
            )
            for box in boxes
        ])
        return transfer

    def dashboard(self, company) -> dict:
        today = timezone.localdate()
        visible = IntercompanyTransfer.objects.filter(
            Q(source_company=company) | Q(destination_company=company)
        )
        today_transfers = visible.filter(created_at__date=today)
        routes = (
            visible
            .values(
                "source_company__code",
                "source_company__name",
                "destination_company__code",
                "destination_company__name",
            )
            .annotate(transfer_count=Count("id"), barcode_count=Sum("total_barcodes"), total_qty=Sum("total_qty"))
            .order_by("-barcode_count")[:10]
        )
        return {
            "today": {
                "transfer_count": today_transfers.count(),
                "barcode_count": today_transfers.aggregate(total=Sum("total_barcodes"))["total"] or 0,
                "carton_count": today_transfers.aggregate(total=Sum("total_barcodes"))["total"] or 0,
                "total_qty": str(today_transfers.aggregate(total=Sum("total_qty"))["total"] or Decimal("0")),
            },
            "routes": [
                {
                    "source_company_code": row["source_company__code"],
                    "source_company_name": row["source_company__name"],
                    "destination_company_code": row["destination_company__code"],
                    "destination_company_name": row["destination_company__name"],
                    "transfer_count": row["transfer_count"],
                    "barcode_count": row["barcode_count"] or 0,
                    "total_qty": str(row["total_qty"] or Decimal("0")),
                }
                for row in routes
            ],
        }
