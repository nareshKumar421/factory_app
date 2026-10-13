"""The GRPO engine, scoped to fixed-asset gate entries.

``FixedAssetGRPOService`` is ``grpo.services.GRPOService`` with
``entry_type="FIXED_ASSET"``. Everything that matters is inherited unchanged:
``save_grpo_draft``, ``post_saved_grpo`` / ``post_grpo`` (the real SAP post),
the preview, the history, ``find_app_posted_grpo`` (the read-back that stops a
second GRPO after a timeout SAP committed anyway). QC is skipped, as for every
entry type other than RAW_MATERIAL. What the subclass adds:

* **Type and scope at every door.** A draft is only saved, and a GRPO only
  posted, for a FIXED_ASSET entry of the company that is not closed as done
  in SAP by hand and did not come in before the cut-over. The engine's own
  ``_load_grpo_draft_targets`` has no type check (the residual in the
  README); this subclass's does.
* **One warehouse per document.** The engine sends one header-level
  ``WarehouseCode`` for every line. A PO whose posting lines sit in different
  warehouses is refused with "post one warehouse at a time"; otherwise the
  lines' own warehouse is the one sent. The engine is not changed.
* **A simulated post blocks a real one** with a message that says what to do,
  instead of the engine's "GRPO already posted ... SAP Doc Num: None".

SIMULATE, AND WHY IT IS NOT A WRAPPED SAP CLIENT. The override document
prefers a wrapper around the engine's SAP client that passes reads and fakes
writes. The engine leaves no hook for one: ``post_grpo`` calls the
module-level name ``grpo.services.SAPClient(...)`` directly (for the open
quantities, the batch flags, the upload and the create), and there is no
accessor a subclass could override. Swapping that module global at run time
would change grpo's behaviour for every thread of the process. So the
documented fallback is taken, made as close to a real post as the engine
allows: ``simulate_saved_grpo`` re-runs every check ``post_grpo`` makes, in
the same order and with the same messages, **reads** SAP where the engine
reads it (open PO quantities for the over-receipt rule, batch-managed flags,
the vendor's BP group), builds the exact document ``post_grpo`` would send,
and then writes nothing to SAP. The draft row itself becomes the POSTED,
simulated posting, with its line postings, its files kept (status PENDING)
and a ``FixedAssetGRPORecord`` holding the document it would have sent. It is
saved with ``update()`` so grpo's post_save signal does not tell the "grpo"
group a GRPO was posted to SAP.

Where the copy must be kept in step with the engine is marked ``MIRRORS
post_grpo`` below. If ``post_grpo`` gains a check, add it there.
"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOLinePosting, GRPOPosting, GRPOStatus
from grpo.services import GRPOService
from raw_material_gatein.models import POItemReceipt, POReceipt
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from weighment.models import Weighment

from ..constants import FA_ENTRY_TYPE, KIND
from .entries import fa_scope, is_before_cutover, needs_po_entries
from .state import (
    UNSENT_STATUSES,
    active_source_ids,
    closure_of,
    is_simulated,
    postings_of_receipt,
    record_fa_post,
)

logger = logging.getLogger(__name__)

#: SAP object type of a purchase order, the GRPO line's BaseType. The same
#: value as ``grpo.services.PURCHASE_ORDER_OBJECT_TYPE``.
PURCHASE_ORDER_OBJECT_TYPE = 22

SIMULATED_MESSAGE = "Simulated: nothing was sent to SAP."


def _decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _json_safe_batches(batches) -> List[Dict[str, Any]]:
    """Batch rows with Decimals as strings, as grpo stores them on a line."""
    return [
        {key: (str(value) if isinstance(value, Decimal) else value) for key, value in batch.items()}
        for batch in (batches or [])
    ]


class FixedAssetGRPOService(GRPOService):
    """``GRPOService`` for FIXED_ASSET gate entries. See the module docstring."""

    def __init__(self, company_code: str):
        super().__init__(company_code, entry_type=FA_ENTRY_TYPE)

    # -- the doors -------------------------------------------------------------

    def assert_entry_postable(self, vehicle_entry) -> None:
        """Refuse an entry this app must not post. Raises ``ValueError``."""
        if (vehicle_entry.entry_type or "RAW_MATERIAL") != FA_ENTRY_TYPE:
            raise ValueError(
                f"Gate entry {vehicle_entry.entry_no} is not a fixed-asset entry; "
                f"post it from its own GRPO screen."
            )
        closure = closure_of(vehicle_entry)
        if closure is not None:
            ref = f" as {closure.sap_doc_num}" if closure.sap_doc_num else ""
            raise ValueError(
                f"Gate entry {vehicle_entry.entry_no} was done in SAP by hand{ref}. "
                f"Reopen it before posting from the app."
            )
        if is_before_cutover(vehicle_entry):
            raise ValueError(
                f"Gate entry {vehicle_entry.entry_no} came in before the Fixed Asset "
                f"GRPO cut-over; it is posted in SAP by hand."
            )

    def _load_grpo_draft_targets(self, vehicle_entry_id, po_receipt_ids):
        vehicle_entry, po_receipts = super()._load_grpo_draft_targets(
            vehicle_entry_id, po_receipt_ids
        )
        self.assert_entry_postable(vehicle_entry)
        return vehicle_entry, po_receipts

    def lines_warehouse(self, po_receipt_ids, items) -> str:
        """The one warehouse the GRPO's lines sit in ("" when none is known).

        The lines that post are every line of the chosen receipts whose
        accepted quantity is above zero: the request's quantity where it names
        the line, else the stored one (``post_grpo`` posts both). Raises
        ``ValueError`` when they sit in more than one warehouse.
        """
        requested = {}
        for item in items or []:
            try:
                requested[int(item["po_item_receipt_id"])] = _decimal(item.get("accepted_qty"))
            except (KeyError, TypeError, ValueError):
                continue
        warehouses = set()
        for line in POItemReceipt.objects.filter(po_receipt_id__in=list(po_receipt_ids or [])):
            qty = requested.get(line.id, line.accepted_qty)
            code = (line.warehouse_code or "").strip()
            if _decimal(qty) > 0 and code:
                warehouses.add(code)
        if len(warehouses) > 1:
            raise ValueError(
                "These PO lines sit in different warehouses ("
                + ", ".join(sorted(warehouses))
                + "). A GRPO goes to one warehouse, so post one warehouse at a time: "
                "link only one warehouse's lines to this entry, or set the others' "
                "quantity to zero."
            )
        return next(iter(warehouses), "")

    def _refuse_simulated(self, po_receipts) -> None:
        for receipt in po_receipts:
            for posting in postings_of_receipt(receipt).filter(status=GRPOStatus.POSTED):
                if is_simulated(posting):
                    raise ValueError(
                        f"A simulated GRPO exists for PO {receipt.po_number}; discard it "
                        f"from Fixed Asset GRPO history before posting for real."
                    )

    def _check_before_post(self, vehicle_entry_id, po_receipt_ids, items, warehouse_code) -> None:
        try:
            vehicle_entry = VehicleEntry.objects.get(
                id=vehicle_entry_id, company__code=self.company_code, is_active=True
            )
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")
        self.assert_entry_postable(vehicle_entry)
        receipts = list(POReceipt.objects.filter(
            id__in=list(po_receipt_ids or []), vehicle_entry=vehicle_entry
        ))
        self._refuse_simulated(receipts)
        lines_whs = self.lines_warehouse(po_receipt_ids, items)
        if lines_whs and (warehouse_code or "").strip() != lines_whs:
            raise ValueError(
                f"The PO lines are in warehouse {lines_whs}, but the GRPO would put them "
                f"in {warehouse_code or '(none)'}. Save the GRPO again from the Fixed "
                f"Asset GRPO screen."
            )

    def post_grpo(self, vehicle_entry_id, po_receipt_ids, user, items, branch_id,
                  warehouse_code=None, **kwargs):
        """The engine's real post, behind this app's checks (a backstop).

        The view and the queue handler check the same things first; this is
        what stops a draft that went stale between saving and sending.
        """
        self._check_before_post(vehicle_entry_id, po_receipt_ids, items, warehouse_code)
        return super().post_grpo(
            vehicle_entry_id,
            po_receipt_ids,
            user,
            items,
            branch_id,
            warehouse_code=warehouse_code,
            **kwargs,
        )

    # -- the lists -------------------------------------------------------------

    def get_pending_grpo_entries(self, year: Optional[int] = None,
                                 month: Optional[int] = None) -> List[VehicleEntry]:
        entries = super().get_pending_grpo_entries(year=year, month=month)
        if not entries:
            return entries
        allowed = set(
            fa_scope(VehicleEntry.objects.filter(id__in=[e.id for e in entries]))
            .values_list("id", flat=True)
        )
        return [entry for entry in entries if entry.id in allowed]

    def get_all_grpo_visible_entries(self, year: Optional[int] = None,
                                     month: Optional[int] = None,
                                     include_closed: bool = False):
        queryset = super().get_all_grpo_visible_entries(year=year, month=month)
        return fa_scope(queryset, include_closed=include_closed)

    def get_grpo_dashboard_summary(self) -> Dict[str, Any]:
        """The FA dashboard numbers, every one of them counted over FA entries only.

        The engine's posting and failed counts are company-wide across entry
        types; these are not. Simulated posts are counted apart from real ones.
        """
        pending_entry_count = 0
        pending_po_count = 0
        for entry in self.get_pending_grpo_entries():
            posted_po_ids = set()
            for grpo in entry.grpo_postings.filter(status=GRPOStatus.POSTED):
                posted_po_ids.update(grpo.po_receipts.values_list("id", flat=True))
                if grpo.po_receipt_id:
                    posted_po_ids.add(grpo.po_receipt_id)
            count = sum(1 for po in entry.po_receipts.all() if po.id not in posted_po_ids)
            if count:
                pending_entry_count += 1
                pending_po_count += count

        item_totals = POItemReceipt.objects.filter(
            is_active=True,
            po_receipt__is_active=True,
            po_receipt__vehicle_entry__company__code=self.company_code,
            po_receipt__vehicle_entry__entry_type=FA_ENTRY_TYPE,
            po_receipt__vehicle_entry__is_active=True,
        ).exclude(
            po_receipt__vehicle_entry__status=GateEntryStatus.CANCELLED,
        ).aggregate(accepted_qty=Sum("accepted_qty"), rejected_qty=Sum("rejected_qty"))

        postings = GRPOPosting.objects.filter(
            vehicle_entry__company__code=self.company_code,
            vehicle_entry__is_active=True,
            vehicle_entry__entry_type=FA_ENTRY_TYPE,
        )
        counts = {key: postings.filter(status=key).count() for key in GRPOStatus.values}
        simulated_count = postings.filter(
            status=GRPOStatus.POSTED, fa_record__simulated=True
        ).count()

        # A simulated post resolves no failure: a real attempt refused while it
        # existed ("A simulated GRPO exists ...") still counts as failed.
        posted_po_ids, posted_entry_ids = self.build_posted_supersede_sets(
            postings.filter(status=GRPOStatus.POSTED)
            .exclude(fa_record__simulated=True)
            .prefetch_related("po_receipts")
        )
        failed_count = sum(
            1
            for posting in postings.filter(status=GRPOStatus.FAILED).prefetch_related("po_receipts")
            if not self.is_failure_superseded(posting, posted_po_ids, posted_entry_ids)
        )

        closed_count = VehicleEntry.objects.filter(
            company__code=self.company_code,
            entry_type=FA_ENTRY_TYPE,
            is_active=True,
            fa_grpo_closure__isnull=False,
        ).count()

        return {
            "pending_entry_count": pending_entry_count,
            "pending_po_count": pending_po_count,
            "qc_accepted_qty": item_totals["accepted_qty"] or Decimal("0"),
            "qc_rejected_qty": item_totals["rejected_qty"] or Decimal("0"),
            "posting_pending_count": counts[GRPOStatus.PENDING],
            "posted_count": counts[GRPOStatus.POSTED] - simulated_count,
            "failed_count": failed_count,
            "partially_posted_count": counts[GRPOStatus.PARTIALLY_POSTED],
            "queued_count": counts[GRPOStatus.QUEUED],
            "draft_count": counts[GRPOStatus.DRAFT],
            "simulated_count": simulated_count,
            "needs_po_count": needs_po_entries(self.company_code).count(),
            "closed_count": closed_count,
        }

    # -- simulate --------------------------------------------------------------

    def simulate_saved_grpo(self, *, grpo_posting_id: int, user) -> GRPOPosting:
        """Run a saved FA GRPO through every check and record it as posted, without SAP writes.

        The twin of ``post_saved_grpo``: same statuses accepted, same payload
        re-validation, same "keep the draft FAILED with its data" on a refusal.
        On success the draft row itself becomes the simulated POSTED posting
        (its id does not change).
        """
        from grpo.serializers import GRPOPostRequestSerializer

        try:
            draft = GRPOPosting.objects.select_related("vehicle_entry").prefetch_related(
                "attachments"
            ).get(id=grpo_posting_id)
        except GRPOPosting.DoesNotExist:
            raise ValueError(f"GRPO posting {grpo_posting_id} not found")

        if draft.status not in (GRPOStatus.DRAFT, GRPOStatus.FAILED, GRPOStatus.QUEUED):
            raise ValueError(
                f"Only draft or failed GRPOs can be posted. This posting is '{draft.status}'."
            )
        if not draft.request_payload:
            raise ValueError(
                "This posting has no saved data to post. "
                "Open the GRPO form, fill it in and save first."
            )

        serializer = GRPOPostRequestSerializer(data=draft.request_payload)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data

        try:
            with transaction.atomic():
                return self._simulate(draft=draft, v=v, user=user)
        except (ValueError, SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            # The atomic block above rolled back whatever it wrote; keep the
            # draft, FAILED, with its payload and files, as post_saved_grpo does.
            GRPOPosting.objects.filter(id=draft.id).update(
                status=GRPOStatus.FAILED,
                error_message=str(exc) or exc.__class__.__name__,
            )
            raise

    def _simulate(self, *, draft: GRPOPosting, v: Dict[str, Any], user) -> GRPOPosting:
        vehicle_entry_id = v["vehicle_entry_id"]
        po_receipt_ids = v["po_receipt_ids"]
        items = v["items"]
        if vehicle_entry_id != draft.vehicle_entry_id:
            raise ValueError("The saved GRPO names a different gate entry from its own.")

        # This app's own checks (type, closure, cut-over, simulated, warehouse).
        self._check_before_post(vehicle_entry_id, po_receipt_ids, items, v.get("warehouse_code"))

        # ---- MIRRORS post_grpo: the engine's checks, in its order -----------
        try:
            vehicle_entry = VehicleEntry.objects.get(
                id=vehicle_entry_id, company__code=self.company_code, is_active=True
            )
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        po_receipts = list(
            POReceipt.objects.prefetch_related("items").filter(
                id__in=po_receipt_ids, vehicle_entry=vehicle_entry
            )
        )
        if len(po_receipts) != len(po_receipt_ids):
            missing = set(po_receipt_ids) - {po.id for po in po_receipts}
            raise ValueError(f"PO receipt(s) not found for this vehicle entry: {missing}")

        supplier_codes = {po.supplier_code for po in po_receipts}
        if len(supplier_codes) > 1:
            raise ValueError(
                f"Cannot merge POs from different suppliers. Found suppliers: {supplier_codes}"
            )
        branch_ids = {po.branch_id for po in po_receipts if po.branch_id is not None}
        if len(branch_ids) > 1:
            raise ValueError(
                f"Cannot merge POs with different branch IDs. Found branch IDs: {branch_ids}"
            )

        if vehicle_entry.status not in [GateEntryStatus.COMPLETED, GateEntryStatus.QC_COMPLETED]:
            raise ValueError(
                f"Gate entry is not completed. Current status: {vehicle_entry.status}"
            )
        # skip_qc is True for FIXED_ASSET, as in the engine.

        tare_weight = v.get("tare_weight")
        if tare_weight is not None:
            # The engine also writes the tare onto the weighment; a simulation
            # writes nothing to the gate's records, so only the checks are made.
            if tare_weight <= 0:
                raise ValueError("Tare weight must be greater than zero")
            weighment = Weighment.objects.filter(vehicle_entry=vehicle_entry).first()
            if (
                weighment
                and weighment.gross_weight is not None
                and weighment.gross_weight > 0
                and tare_weight > weighment.gross_weight
            ):
                raise ValueError("Tare weight cannot be greater than gross weight")

        for po_receipt in po_receipts:
            existing = postings_of_receipt(po_receipt).filter(status=GRPOStatus.POSTED).first()
            if existing:
                raise ValueError(
                    f"GRPO already posted for PO {po_receipt.po_number}. "
                    f"SAP Doc Num: {existing.sap_doc_num}"
                )

        all_po_item_ids = set()
        for po_receipt in po_receipts:
            all_po_item_ids.update(po_receipt.items.values_list("id", flat=True))
        items_input_map = {item["po_item_receipt_id"]: item for item in items}
        invalid_ids = set(items_input_map) - all_po_item_ids
        if invalid_ids:
            raise ValueError(f"Invalid PO item receipt IDs: {invalid_ids}")

        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.id in items_input_map:
                    accepted_qty = items_input_map[item.id]["accepted_qty"]
                    item.accepted_qty = accepted_qty
                    item.rejected_qty = max(item.received_qty - accepted_qty, Decimal("0"))
                    item.save()

        warehouse_code = v.get("warehouse_code")
        document_lines: List[Dict[str, Any]] = []
        grpo_lines_data: List[Dict[str, Any]] = []
        batch_flags = self._get_batch_managed_flags(
            item.po_item_code for po_receipt in po_receipts for item in po_receipt.items.all()
        )
        batch_errors: List[str] = []
        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.accepted_qty <= 0:
                    continue
                item_input = items_input_map.get(item.id, {})
                line_data: Dict[str, Any] = {
                    "ItemCode": item.po_item_code,
                    "Quantity": str(item.accepted_qty),
                }
                if po_receipt.sap_doc_entry and item.sap_line_num is not None:
                    line_data["BaseEntry"] = po_receipt.sap_doc_entry
                    line_data["BaseLine"] = item.sap_line_num
                    line_data["BaseType"] = PURCHASE_ORDER_OBJECT_TYPE
                if warehouse_code:
                    line_data["WarehouseCode"] = warehouse_code
                unit_price = item_input.get("unit_price")
                if unit_price is not None:
                    line_data["UnitPrice"] = float(unit_price)
                tax_code = item_input.get("tax_code")
                if tax_code:
                    line_data["TaxCode"] = tax_code
                gl_account = item_input.get("gl_account")
                if gl_account:
                    line_data["AccountCode"] = gl_account
                variety = (item_input.get("variety") or item.variety or "").strip()
                if variety:
                    line_data["U_Variety"] = str(variety)[:50]
                    line_data["CostingCode"] = variety
                try:
                    batch_numbers = self._build_line_batch_numbers(
                        item_code=item.po_item_code,
                        item_name=item.item_name,
                        accepted_qty=item.accepted_qty,
                        batches=item_input.get("batches"),
                        is_batch_managed=batch_flags.get(item.po_item_code),
                        line_index=len(document_lines),
                    )
                except ValueError as exc:
                    batch_errors.append(str(exc))
                    batch_numbers = []
                if batch_numbers:
                    line_data["BatchNumbers"] = batch_numbers
                document_lines.append(line_data)
                grpo_lines_data.append({
                    "po_item_receipt": item,
                    "quantity_posted": item.accepted_qty,
                    "base_entry": po_receipt.sap_doc_entry,
                    "base_line": item.sap_line_num,
                    "batches": batch_numbers,
                })

        if not document_lines:
            raise ValueError("No accepted quantities to post")
        if batch_errors:
            raise ValueError(" ".join(batch_errors))

        # Reads SAP's live open quantities, exactly as a real post does.
        self._validate_over_receipt_tolerance(po_receipts[0].supplier_code, grpo_lines_data)

        payload: Dict[str, Any] = {
            "CardCode": po_receipts[0].supplier_code,
            "BPL_IDAssignedToInvoice": v["branch_id"],
            "Comments": self._build_structured_comments(
                user, po_receipts, vehicle_entry, v.get("comments")
            ),
            "DocumentLines": document_lines,
        }
        for key, field in (("DocDate", "doc_date"), ("DocDueDate", "doc_due_date"),
                           ("TaxDate", "tax_date")):
            if v.get(field):
                payload[key] = str(v[field])
        if v.get("should_roundoff"):
            subtotal = sum(
                (_decimal(line.get("Quantity")) * _decimal(line.get("UnitPrice"))
                 for line in document_lines),
                Decimal("0"),
            )
            if subtotal > 0:
                rounded = subtotal.quantize(Decimal("1"), rounding="ROUND_HALF_UP")
                if rounded - subtotal != 0:
                    payload["RoundDif"] = float(rounded - subtotal)
        if v.get("vendor_ref"):
            payload["NumAtCard"] = v["vendor_ref"]
        if v.get("extra_charges"):
            payload["DocumentAdditionalExpenses"] = [
                self._build_additional_expense(charge) for charge in v["extra_charges"]
            ]
        # ---- end MIRRORS post_grpo ---------------------------------------------

        files = [att.original_filename for att in draft.attachments.all()]
        total = sum(
            (_decimal(line.get("Quantity")) * _decimal(line.get("UnitPrice"))
             for line in document_lines),
            Decimal("0"),
        ).quantize(Decimal("0.01"))
        now = timezone.now()

        # update(), not save(): grpo's post_save signal would tell the "grpo"
        # group that a GRPO was posted to SAP, and none was.
        GRPOPosting.objects.filter(id=draft.id).update(
            status=GRPOStatus.POSTED,
            po_receipt=po_receipts[0],
            sap_doc_entry=None,
            sap_doc_num=None,
            sap_doc_total=total,
            posted_at=now,
            posted_by=user if getattr(user, "pk", None) else None,
            error_message=SIMULATED_MESSAGE,
            updated_at=now,
        )
        draft.po_receipts.set(po_receipts)
        for line in grpo_lines_data:
            GRPOLinePosting.objects.create(
                grpo_posting_id=draft.id,
                po_item_receipt=line["po_item_receipt"],
                quantity_posted=line["quantity_posted"],
                base_entry=line["base_entry"],
                base_line=line["base_line"],
                batches=_json_safe_batches(line.get("batches")),
            )
        record_fa_post(
            draft,
            user=user,
            simulated=True,
            payload={
                "endpoint": "POST /b1s/v2/PurchaseDeliveryNotes",
                "document": payload,
                "attachment_files": files,
                "note": (
                    "Simulated. Not sent. A real post uploads the files to "
                    "Attachments2 first and adds AttachmentEntry."
                ),
            },
        )
        draft.refresh_from_db()
        return draft


# -- escape hatches -------------------------------------------------------------

def _require_fa_posting(posting: GRPOPosting) -> None:
    if (posting.vehicle_entry.entry_type or "") != FA_ENTRY_TYPE:
        raise ValueError("This GRPO is not a fixed-asset GRPO.")


@transaction.atomic
def discard_simulated_grpo(*, posting: GRPOPosting, user) -> None:
    """Delete a simulated post so the entry can be posted for real.

    Puts the accepted quantity of **every** line of the posting's PO receipts
    back to what was received (a fixed asset has no QC). Simulate writes the
    request's quantity onto every line it names, the ones sent at zero too
    (the README's mixed-warehouse workaround), and those have no line posting
    to find them by. Then deletes the line postings, the files and the
    posting. Nothing exists in SAP, so nothing is cancelled there.

    The SAP postings row that recorded the simulated post (kind
    ``fixed_asset_grpo.post``, POSTED) is marked CANCELLED with the reason and
    its link cleared, so the log no longer shows a "posted" fixed-asset GRPO
    whose page is gone. Only that row's data changes; ``sap_postings`` is not
    edited.
    """
    posting = (
        GRPOPosting.objects.select_for_update(of=("self",))
        .select_related("vehicle_entry")
        .get(pk=posting.pk)
    )
    _require_fa_posting(posting)
    if posting.status != GRPOStatus.POSTED or not is_simulated(posting):
        raise ValueError("Only a simulated GRPO can be discarded; this one is real or not posted.")
    if posting.sap_doc_entry:
        raise ValueError("This GRPO has an SAP document; it cannot be discarded here.")

    receipt_ids = set(posting.po_receipts.values_list("id", flat=True))
    if posting.po_receipt_id:
        receipt_ids.add(posting.po_receipt_id)
    receipt_ids.update(
        posting.lines.values_list("po_item_receipt__po_receipt_id", flat=True)
    )
    for item in POItemReceipt.objects.filter(po_receipt_id__in=receipt_ids):
        item.accepted_qty = item.received_qty
        item.rejected_qty = 0
        item.updated_by = user if getattr(user, "pk", None) else item.updated_by
        item.save()
    for att in posting.attachments.all():
        if att.file:
            att.file.delete(save=False)
    _cancel_simulated_queue_rows(posting, user)
    logger.info("Simulated FA GRPO %s discarded by %s", posting.id, user)
    posting.delete()


def _cancel_simulated_queue_rows(posting: GRPOPosting, user) -> int:
    """Mark the queue rows that recorded this simulated post as cancelled.

    A direct update of ``sap_postings`` rows, because its ``cancel()`` only
    takes a row that is still waiting or was refused, and this one is POSTED.
    Only rows whose result says *simulated* are touched, so a real post's row
    can never be. Returns how many rows changed.
    """
    from sap_postings.models import SapPosting, SapPostingStatus

    rows = list(
        SapPosting.objects.select_for_update().filter(
            kind=KIND, source_id=posting.id, status=SapPostingStatus.POSTED
        )
    )
    changed = 0
    for row in rows:
        result = dict(row.result or {})
        if not result.get("simulated"):
            continue
        result["discarded"] = True
        row.status = SapPostingStatus.CANCELLED
        row.cancelled_by = user if getattr(user, "pk", None) else None
        row.cancel_reason = (
            f"Simulated post discarded from Fixed Asset GRPO history; nothing was "
            f"ever sent to SAP (GRPO posting {posting.id} no longer exists)."
        )
        row.link = ""
        row.result = result
        row.save(
            update_fields=[
                "status", "cancelled_by", "cancel_reason", "link", "result", "updated_at",
            ]
        )
        changed += 1
    return changed


@transaction.atomic
def discard_fa_draft(*, posting: GRPOPosting, user) -> None:
    """Delete an unsent FA GRPO draft and its files.

    Unsent: DRAFT, FAILED, or QUEUED with no live SAP postings row (its row was
    cancelled, so nothing will send it), and no SAP document.
    """
    posting = (
        GRPOPosting.objects.select_for_update(of=("self",))
        .select_related("vehicle_entry")
        .get(pk=posting.pk)
    )
    _require_fa_posting(posting)
    if posting.status not in UNSENT_STATUSES or posting.sap_doc_entry:
        raise ValueError("Only an unposted draft or failed GRPO can be discarded.")
    if posting.id in active_source_ids([posting.id]):
        raise ValueError("This GRPO is on its way to SAP; it cannot be discarded now.")
    for att in posting.attachments.all():
        if att.file:
            att.file.delete(save=False)
    logger.info("FA GRPO draft %s discarded by %s", posting.id, user)
    posting.delete()
