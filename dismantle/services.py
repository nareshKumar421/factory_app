"""Building and posting a dismantle.

Reading order for anyone changing this file: ``models.py`` for what a dismantle
is, ``guards.py`` for the SAP rules it has to satisfy, then ``post`` below --
which is where the three documents are written, in the one order SAP accepts.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from company.models import Company
from goods_return.models import GoodsReturnItem, GoodsReturnStatus

from . import guards
from .models import (
    Dismantle,
    DismantleComponent,
    DismantleSource,
    DismantleStatus,
)

logger = logging.getLogger(__name__)

#: SAP object type of a production order — what a completion document's lines
#: point at through ``BaseType``.
PRODUCTION_ORDER_OBJECT_TYPE = 202

#: The return statuses whose stock is actually in SAP and therefore dismantlable.
#: A return that has not posted has no stock in the warehouse to take apart.
POSTED_RETURN_STATUSES = (
    GoodsReturnStatus.POSTED,
    GoodsReturnStatus.PARTIALLY_POSTED,
)

#: A dismantle that is finished or abandoned is not editable.
EDITABLE_STATUSES = (DismantleStatus.DRAFT, DismantleStatus.PARTIALLY_POSTED)


def _client(company_code: str):
    from sap_client.client import SAPClient

    return SAPClient(company_code=company_code)


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


class DismantleService:
    """Everything a dismantle does, from picking the stock to closing the order."""

    def __init__(self, company: Company | None = None):
        self.company = company

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def list_dismantles(self, company_ids, *, status=None, search=None):
        qs = (
            Dismantle.objects.filter(company_id__in=company_ids, is_active=True)
            .select_related("company", "goods_return")
            .prefetch_related("components")
        )
        if status:
            qs = qs.filter(status=status)
        if search:
            qs = qs.filter(
                Q(entry_no__icontains=search)
                | Q(item_code__icontains=search)
                | Q(item_name__icontains=search)
                | Q(batch_number__icontains=search)
                | Q(sap_order_doc_num__icontains=search)
            )
        return qs

    def get_dismantle(self, pk, allowed_company_ids) -> Dismantle:
        record = (
            Dismantle.objects.select_related("company", "goods_return")
            .prefetch_related("components")
            .filter(pk=pk, is_active=True)
            .first()
        )
        if record is None:
            raise ValueError("Dismantle not found.")
        if record.company_id not in allowed_company_ids:
            raise PermissionDenied(
                "This record belongs to a company you cannot access."
            )
        return record

    def dismantlable_stock(self, warehouse_code, *, search="", limit=50):
        """What is in the warehouse that SAP could take apart."""
        warehouse_code = guards.check_warehouse(warehouse_code)
        return _client(self.company.code).dismantlable_stock(
            warehouse_code, search=search, limit=limit
        )

    def available_batches(self, item_code, warehouse_code):
        """The batches of one item held in the warehouse, oldest first."""
        warehouse_code = guards.check_warehouse(warehouse_code)
        return _client(self.company.code).available_batches(item_code, warehouse_code)

    def warehouses(self):
        """Every active warehouse, the goods-return ones first.

        Not only the ``-GR`` warehouses. Returned stock is the common case and so
        sorts to the top, but live SAP raises disassembly orders out of ``BH-PF``,
        ``BH-FG`` and ``BH-WST`` as well — in Oil, more of them than out of
        ``BH-GR`` — so a picker limited to returns warehouses would hide most of
        what actually happens on the floor.
        """
        client = _client(self.company.code)
        return_codes = {w.warehouse_code for w in client.get_return_warehouses()}
        return [
            {
                "warehouse_code": w.warehouse_code,
                "warehouse_name": w.warehouse_name,
                "is_return_warehouse": w.warehouse_code in return_codes,
            }
            for w in sorted(
                client.get_active_warehouses(),
                key=lambda w: (w.warehouse_code not in return_codes, w.warehouse_name),
            )
        ]

    # ------------------------------------------------------------------
    # the return-driven source
    # ------------------------------------------------------------------

    def returned_lines(self, company_ids, *, search=None, limit=200):
        """Returned stock still waiting to be dealt with, line by line.

        Only lines off returns SAP actually took: an unposted return has no stock
        in the warehouse, so offering it would let someone dismantle goods that
        are not there. Lines already dismantled in full are left out -- the
        quantity offered is what is left, not what came back.

        The batch comes back from SAP rather than being recomputed (see
        ``_match_return_batch``), so an app-booked return needs nothing re-keyed
        here. Returns typed straight into SAP by the accounts team do not appear;
        those are dismantled from warehouse stock instead.
        """
        lines = (
            GoodsReturnItem.objects.filter(
                goods_return__company_id__in=company_ids,
                goods_return__status__in=POSTED_RETURN_STATUSES,
                goods_return__is_active=True,
                is_active=True,
                return_quantity__gt=0,
            )
            .select_related("goods_return", "invoice_ref")
            .order_by("-goods_return__received_at", "id")
        )
        if search:
            lines = lines.filter(
                Q(item_code__icontains=search)
                | Q(item_name__icontains=search)
                | Q(goods_return__entry_no__icontains=search)
                | Q(goods_return__customer_name__icontains=search)
            )

        lines = list(lines[: int(limit or 200)])
        already = self._dismantled_quantities([line.pk for line in lines])
        batches = self._return_batches(lines)

        result = []
        for line in lines:
            gr = line.goods_return
            # Only lines whose own invoice actually posted have stock in SAP. On a
            # partly-posted return the refused invoices' lines do not.
            ref = line.invoice_ref
            if ref is not None and not ref.is_posted:
                continue
            if gr.status == GoodsReturnStatus.PARTIALLY_POSTED and ref is None:
                continue

            remaining = _decimal(line.return_quantity) - already.get(line.pk, Decimal(0))
            if remaining <= 0:
                continue
            batch, in_sap = self._match_return_batch(batches, gr, line)
            result.append(
                {
                    "goods_return_id": gr.pk,
                    "goods_return_item_id": line.pk,
                    "entry_no": gr.entry_no,
                    "customer_code": gr.customer_code,
                    "customer_name": gr.customer_name,
                    "received_at": gr.received_at,
                    "warehouse_code": gr.sap_return_warehouse,
                    "item_code": line.item_code,
                    "item_name": line.item_name,
                    "uom": line.uom,
                    "condition": line.condition,
                    "returned_quantity": line.return_quantity,
                    "remaining_quantity": remaining,
                    # The batch the stock is actually sitting in, read back from
                    # SAP rather than recomputed -- see ``_match_return_batch``.
                    "batch_number": batch,
                    # What SAP holds in that batch right now. Shown next to the
                    # app's own remaining figure because the two can disagree --
                    # somebody dismantling in SAP directly moves one and not the
                    # other -- and a picker that only ever showed the app's
                    # arithmetic would send the operator to a post-time refusal.
                    "sap_quantity": in_sap,
                    "original_batch_number": line.original_batch_number,
                }
            )
        return result

    @staticmethod
    def _return_batches(lines) -> list:
        """Every goods-return batch holding stock behind these lines.

        One read per company, not per line: the picker lists up to 200 lines and
        a round-trip each would make opening the page a minute's work. Grouped by
        company because each has its own SAP database — reading Oil's warehouses
        out of the Mart company would answer about different stock entirely.
        """
        by_company: dict = {}
        for line in lines:
            gr = line.goods_return
            if gr.sap_return_warehouse:
                by_company.setdefault(gr.company.code, set()).add(gr.sap_return_warehouse)

        rows = []
        for company_code, warehouses in by_company.items():
            for row in _client(company_code).return_batches(sorted(warehouses)):
                rows.append({**row, "company_code": company_code})
        return rows

    def _ensure_return_batch(self, record) -> None:
        """Re-resolve a return-sourced record's batch when the stored one is empty.

        Drafts raised before the batch was read back from SAP carry a recomputed
        name, and for a return booked under the old numbering that batch does not
        exist -- the record is stuck on "SAP holds 0 of this batch" for ever,
        through no fault of the operator. Rather than make them delete and start
        again, the batch is resolved afresh whenever the stored one holds nothing.

        Only ever while the goods issue is still unposted: once SAP has consumed
        the batch, the record must keep naming the one that was actually used.
        """
        if record.source != DismantleSource.GOODS_RETURN or not record.goods_return_item_id:
            return
        if record.sap_issue_doc_entry:
            return

        line = record.goods_return_item
        gr = record.goods_return
        batches = self._return_batches([line])
        stored_has_stock = any(
            row["batch_number"] == record.batch_number
            and row["item_code"] == record.item_code
            and row["warehouse_code"] == record.warehouse_code
            for row in batches
        )
        if stored_has_stock:
            return

        resolved, _quantity = self._match_return_batch(batches, gr, line)
        if resolved and resolved != record.batch_number:
            logger.info(
                "Dismantle %s: batch %s holds nothing; using %s, which is what SAP "
                "has for %s.",
                record.entry_no,
                record.batch_number,
                resolved,
                gr.entry_no,
            )
            record.batch_number = resolved
            record.save(update_fields=["batch_number", "updated_at"])

    @staticmethod
    def _match_return_batch(batches, gr, line):
        """Which SAP batch this returned line is sitting in, and how much is left.

        The number is NOT recomputed. ``goods_return`` derives it from the entry
        number, but the formula changed -- returns posted before the fix number
        their lines by position (``GR-20260827-0001-0``), ones after by the line's
        database id (``GR-20260827-0001-18``) -- so recomputing invents a batch
        that does not exist for every return booked before the change. What is in
        SAP is the only answer that is true for both.

        Matched by the return's entry number and the item. A return posts one
        document per invoice and SAP refuses duplicate item lines on one document
        (160020), so at most one batch per item per invoice: where two invoices on
        one return brought the same item back, the computed name breaks the tie,
        and failing that the line is left without a batch for the operator to
        pick rather than guessed at.
        """
        from goods_return import guards as return_guards

        computed = return_guards.batch_number_for(gr.entry_no, line.pk)
        prefix = f"{gr.entry_no}-"
        candidates = [
            row
            for row in batches
            if row["item_code"] == line.item_code
            and row["warehouse_code"] == gr.sap_return_warehouse
            and row.get("company_code", gr.company.code) == gr.company.code
            and row["batch_number"].startswith(prefix)
        ]
        for row in candidates:
            if row["batch_number"] == computed:
                return computed, _decimal(row["quantity"])
        if len(candidates) == 1:
            return candidates[0]["batch_number"], _decimal(candidates[0]["quantity"])
        if not candidates:
            # Nothing in SAP under this return: the stock is gone, or the return
            # never really posted. Hand back the computed name so the guard names
            # a batch rather than complaining about a blank one.
            return computed, Decimal(0)
        return "", None

    @staticmethod
    def _dismantled_quantities(line_ids) -> dict:
        """How much of each returned line is already spoken for.

        Drafts count. A draft is a quantity someone has claimed and is about to
        post, and letting a second draft claim the same pieces would produce two
        disassembly orders for stock that only exists once -- the second failing
        at SAP, after the first of its three documents is already in.
        """
        if not line_ids:
            return {}
        rows = Dismantle.objects.filter(
            goods_return_item_id__in=list(line_ids),
            is_active=True,
        ).exclude(status=DismantleStatus.CANCELLED)
        totals: dict = {}
        for row in rows:
            totals[row.goods_return_item_id] = totals.get(
                row.goods_return_item_id, Decimal(0)
            ) + _decimal(row.quantity)
        return totals

    # ------------------------------------------------------------------
    # create / edit
    # ------------------------------------------------------------------

    @transaction.atomic
    def create(self, data, user) -> Dismantle:
        """Start a dismantle and explode the recipe into its component lines.

        The BOM is read and stored now rather than at post time so the operator
        edits a concrete list -- and so the quantities that reach SAP are the ones
        that were on screen, not a fresh explosion that may have changed under
        them between opening the page and posting it.
        """
        company = self.company
        source = data.get("source") or DismantleSource.GOODS_RETURN

        return_line = None
        goods_return = None
        if source == DismantleSource.GOODS_RETURN:
            return_line, goods_return = self._resolve_return_line(
                data.get("goods_return_item_id"), company
            )
            warehouse_code = goods_return.sap_return_warehouse
            item_code = return_line.item_code
        else:
            warehouse_code = data.get("warehouse_code") or ""
            item_code = data.get("item_code") or ""

        warehouse_code = guards.check_warehouse(warehouse_code)
        client = _client(company.code)

        parent = client.dismantle_parent_info(item_code)
        components = client.dismantle_bom(item_code)
        guards.check_parent(item_code, parent, components)

        quantity = _decimal(data.get("quantity"))
        batch_number = (data.get("batch_number") or "").strip()
        if source == DismantleSource.GOODS_RETURN:
            # Read back, never recomputed — the returns module's batch-numbering
            # formula changed, so the name cannot be derived for older returns.
            batch_number = batch_number or self._match_return_batch(
                self._return_batches([return_line]), goods_return, return_line
            )[0]
            remaining = _decimal(return_line.return_quantity) - self._dismantled_quantities(
                [return_line.pk]
            ).get(return_line.pk, Decimal(0))
            guards.check_quantity(quantity, remaining, batch=f"return {goods_return.entry_no}")
        else:
            guards.check_quantity(quantity)

        guards.check_batch(item_code, parent["is_batch_managed"], batch_number)

        record = Dismantle.objects.create(
            company=company,
            entry_no=Dismantle.generate_entry_no(),
            source=source,
            goods_return=goods_return,
            goods_return_item=return_line,
            warehouse_code=warehouse_code,
            item_code=item_code,
            item_name=parent["item_name"],
            uom=parent["uom"],
            batch_number=batch_number if parent["is_batch_managed"] else "",
            quantity=quantity,
            pieces_per_box=_decimal(parent["pieces_per_box"]),
            bom_batch_size=parent["bom_batch_size"],
            remarks=data.get("remarks") or "",
            created_by=user,
            updated_by=user,
        )
        self._build_components(record, components, user)
        return record

    @transaction.atomic
    def create_many(self, rows, user) -> list:
        """Several dismantles in one go — **one record per item**.

        The floor picks a handful of things to take apart in one session, but SAP
        has no notion of a multi-item disassembly: an order names one parent item,
        and its completion documents are that order's. So a basket of five items
        is five records and, later, five sets of three documents.

        All or nothing. A basket where the third row is refused writes none of
        them: the operator fixes the row and sends the basket again, rather than
        hunting for which two of five were created.
        """
        rows = list(rows or [])
        if not rows:
            raise ValueError("Pick at least one item to take apart.")
        return [self.create(row, user) for row in rows]

    def _resolve_return_line(self, line_id, company):
        line = (
            GoodsReturnItem.objects.select_related("goods_return")
            .filter(pk=line_id, is_active=True)
            .first()
        )
        if line is None:
            raise ValueError("That returned line no longer exists.")
        goods_return = line.goods_return
        if goods_return.company_id != company.pk:
            raise ValueError(
                f"{goods_return.entry_no} belongs to another company; a dismantle "
                f"is posted into the company that owns the stock."
            )
        if goods_return.status not in POSTED_RETURN_STATUSES:
            raise ValueError(
                f"{goods_return.entry_no} is not in SAP yet ("
                f"{goods_return.get_status_display()}), so there is no stock to "
                f"take apart. Receive the return first."
            )
        if not goods_return.sap_return_warehouse:
            raise ValueError(
                f"{goods_return.entry_no} has no goods-return warehouse recorded, "
                f"so where the stock sits is unknown."
            )
        return line, goods_return

    def _build_components(self, record: Dismantle, components, user) -> None:
        """Store the exploded recipe, then mint a batch for what needs one.

        The batch is minted in a second pass because it is derived from the
        component's own id, which only exists once the row is written.
        """
        quantity = _decimal(record.quantity)
        rows = [
            DismantleComponent(
                dismantle=record,
                item_code=component["item_code"],
                item_name=component["item_name"],
                uom=component["uom"],
                qty_per_piece=_decimal(component["qty_per_piece"]),
                quantity=_decimal(component["qty_per_piece"]) * quantity,
                warehouse_code=record.warehouse_code,
                is_batch_managed=component["is_batch_managed"],
                created_by=user,
                updated_by=user,
            )
            for component in components
        ]
        DismantleComponent.objects.bulk_create(rows)

        minted = []
        for component in record.components.all():
            if component.is_batch_managed and not component.batch_number:
                component.batch_number = guards.batch_number_for(
                    record.entry_no, component.pk
                )
                minted.append(component)
        if minted:
            DismantleComponent.objects.bulk_update(minted, ["batch_number"])

    @transaction.atomic
    def update_header(self, pk, data, user, allowed_company_ids) -> Dismantle:
        """Change the quantity or remarks; a new quantity re-scales the recipe."""
        record = self.get_dismantle(pk, allowed_company_ids)
        self._assert_editable(record)

        if "quantity" in data:
            quantity = _decimal(data.get("quantity"))
            if record.source == DismantleSource.GOODS_RETURN and record.goods_return_item_id:
                claimed = self._dismantled_quantities([record.goods_return_item_id]).get(
                    record.goods_return_item_id, Decimal(0)
                ) - _decimal(record.quantity)
                remaining = _decimal(record.goods_return_item.return_quantity) - claimed
                guards.check_quantity(quantity, remaining, batch=record.batch_number)
            else:
                guards.check_quantity(quantity)
            record.quantity = quantity
            # Re-scale every line that has not been hand-edited away from the
            # recipe. Quantities the operator typed are left alone -- they were a
            # statement about what actually came back, and silently overwriting
            # them would lose it.
            for component in record.active_components:
                component.quantity = _decimal(component.qty_per_piece) * quantity
                component.updated_by = user
            DismantleComponent.objects.bulk_update(
                record.active_components, ["quantity", "updated_by"]
            )

        if "remarks" in data:
            record.remarks = data.get("remarks") or ""
        if "batch_number" in data:
            record.batch_number = (data.get("batch_number") or "").strip()

        record.updated_by = user
        record.save()
        return record

    @transaction.atomic
    def save_components(self, pk, lines, user, allowed_company_ids) -> Dismantle:
        """Set the recovered quantities -- what actually came back off the floor."""
        record = self.get_dismantle(pk, allowed_company_ids)
        self._assert_editable(record)

        by_id = {component.pk: component for component in record.active_components}
        changed = []
        for line in lines or []:
            component = by_id.get(line.get("id"))
            if component is None:
                continue
            if "quantity" in line:
                component.quantity = _decimal(line.get("quantity"))
            if "recovered" in line:
                component.recovered = bool(line.get("recovered"))
            if "warehouse_code" in line and line.get("warehouse_code"):
                component.warehouse_code = str(line["warehouse_code"]).strip()
            component.updated_by = user
            changed.append(component)

        if changed:
            DismantleComponent.objects.bulk_update(
                changed, ["quantity", "recovered", "warehouse_code", "updated_by"]
            )
        record.updated_by = user
        record.save(update_fields=["updated_by", "updated_at"])
        return record

    @transaction.atomic
    def rebuild_components(self, pk, user, allowed_company_ids) -> Dismantle:
        """Throw the component lines away and explode the recipe again.

        For when the BOM was corrected in SAP after the dismantle was started.
        Refused once anything is in SAP: the order in SAP holds the old lines, and
        a receipt built from a different set would not match it.
        """
        record = self.get_dismantle(pk, allowed_company_ids)
        if record.sap_order_doc_entry:
            raise ValueError(
                f"The disassembly order is already in SAP ("
                f"{record.sap_order_doc_num}), and its component lines cannot be "
                f"rebuilt from here. Finish or cancel this dismantle instead."
            )
        self._assert_editable(record)

        client = _client(record.company.code)
        parent = client.dismantle_parent_info(record.item_code)
        components = client.dismantle_bom(record.item_code)
        guards.check_parent(record.item_code, parent, components)

        record.components.all().delete()
        record.pieces_per_box = _decimal(parent["pieces_per_box"])
        record.bom_batch_size = parent["bom_batch_size"]
        record.updated_by = user
        record.save(
            update_fields=["pieces_per_box", "bom_batch_size", "updated_by", "updated_at"]
        )
        self._build_components(record, components, user)
        return record

    def _assert_editable(self, record: Dismantle) -> None:
        if record.status not in EDITABLE_STATUSES:
            raise ValueError(
                f"A {record.get_status_display().lower()} dismantle cannot be edited."
            )

    @transaction.atomic
    def delete_draft(self, pk, user, allowed_company_ids) -> Dismantle:
        """Throw away a draft the app has not put into SAP.

        A **soft** delete: the row is deactivated and marked cancelled, so it
        leaves every screen while the record of what somebody started — and the
        quantity it had claimed off a returned line — stays on file.

        Refused the moment any of the three documents exists. None of them can be
        withdrawn from here (SAP restricts cancelling these to named users), so a
        deleted record would claim the stock is untouched when it has already
        moved.
        """
        record = self.get_dismantle(pk, allowed_company_ids)
        if record.sap_order_doc_entry:
            raise ValueError(
                f"This dismantle is already in SAP (order "
                f"{record.sap_order_doc_num or record.sap_order_doc_entry}) and "
                f"cannot be deleted from here. Ask the SAP team to close it."
            )
        record.is_active = False
        record.status = DismantleStatus.CANCELLED
        record.updated_by = user
        record.save(
            update_fields=["is_active", "status", "updated_by", "updated_at"]
        )
        return record

    # ------------------------------------------------------------------
    # posting
    # ------------------------------------------------------------------

    def preview(self, pk, allowed_company_ids) -> dict:
        """Everything that would be checked at post time, without posting.

        Same guards, same reads; it is what the screen shows before the operator
        commits, so a dismantle that would be refused says so while it can still
        be fixed.
        """
        record = self.get_dismantle(pk, allowed_company_ids)
        client = _client(record.company.code)
        self._ensure_return_batch(record)

        warnings = []
        errors = []
        inflation = guards.bom_inflation_warning(
            record.item_code, record.pieces_per_box, record.bom_batch_size
        )
        if inflation:
            warnings.append(inflation)

        available = None
        try:
            available = self._available_quantity(client, record)
            variety = self._variety_for(client, record)
            self._run_guards(record, variety, available, client)
        except (guards.DismantleGuardError, ValueError) as exc:
            errors.append(str(exc))

        return {
            "entry_no": record.entry_no,
            "available_quantity": available,
            "warnings": warnings,
            "errors": errors,
            "can_post": not errors,
        }

    @transaction.atomic
    def post(self, pk, user, allowed_company_ids) -> Dismantle:
        """Write the three SAP documents, in the only order SAP accepts.

        1. the disassembly order
        2. **Receipt from Production** — the components come back
        3. **Issue for Production** — the parent is consumed

        Two and three are not interchangeable: SAP refuses the issue while the
        receipt is missing (``20206 Cannot add Goods Issue: no Goods Receipt
        posted for Disassembly Order N``), in all three companies. So the
        components are received first even though the parent is the thing being
        consumed, which reads backwards and is nevertheless correct.

        Every guard runs before the first write. After that each document is
        recorded the instant SAP takes it: nothing here can be withdrawn, so a run
        that fails on document three keeps documents one and two and comes back as
        ``PARTIALLY_POSTED``. Re-posting such a record resumes at the first
        document SAP does not have — it never writes one twice.
        """
        record = (
            Dismantle.objects.select_for_update(of=("self",))
            .select_related("company")
            .prefetch_related("components")
            .filter(pk=pk, is_active=True)
            .first()
        )
        if record is None:
            raise ValueError("Dismantle not found.")
        if record.company_id not in allowed_company_ids:
            raise PermissionDenied("This record belongs to a company you cannot access.")
        if record.status == DismantleStatus.CANCELLED:
            raise ValueError("This dismantle was cancelled.")
        if record.status == DismantleStatus.POSTED:
            raise ValueError("This dismantle is already posted to SAP.")

        client = _client(record.company.code)
        self._ensure_return_batch(record)
        posting_date = timezone.localdate()
        guards.check_posting_date(posting_date)

        variety = self._variety_for(client, record)
        available = self._available_quantity(client, record)
        recovered = self._run_guards(record, variety, available, client)

        record.variety_code = variety
        record.posting_date = posting_date

        try:
            self._post_order(client, record, recovered, posting_date, user)
            self._post_receipt(client, record, recovered, posting_date, variety, user)
            self._post_issue(client, record, posting_date, variety, user)
        except Exception as exc:
            # Nothing reached SAP: raise, and let the transaction roll the draft
            # back to how it was.
            if not record.sap_order_doc_entry:
                raise ValueError(f"SAP refused the dismantle: {exc}") from exc
            # Something is in SAP. Keep what was accepted and report the rest --
            # raising here would roll back the app's record of documents that
            # exist and nobody here can cancel.
            logger.error(
                "Dismantle %s stopped part-way through SAP: %s", record.entry_no, exc
            )
            record.status = DismantleStatus.PARTIALLY_POSTED
            record.sap_post_error = str(exc)
            record.updated_by = user
            record.save()
            record.posting_error = str(exc)
            return record

        record.sap_post_error = ""
        record.status = DismantleStatus.POSTED
        record.posted_by = record.posted_by or user
        record.posted_at = record.posted_at or timezone.now()
        record.updated_by = user

        # Closing is cosmetic — the stock has moved either way — so a failure here
        # is logged and carried on the record rather than failing the dismantle.
        try:
            client.close_production_order(record.sap_order_doc_entry)
            record.order_closed = True
        except Exception as exc:
            logger.warning(
                "Dismantle %s posted but its order could not be closed: %s",
                record.entry_no,
                exc,
            )
            record.sap_post_error = (
                f"Posted. SAP would not close disassembly order "
                f"{record.sap_order_doc_num}: {exc}"
            )

        record.save()
        record.posting_error = ""
        return record

    # -- the individual writes ------------------------------------------------

    def _post_order(self, client, record, recovered, posting_date, user) -> None:
        """Document 1: the disassembly order, plus SAP's line numbers read back."""
        if record.sap_order_doc_entry:
            return

        date_text = posting_date.isoformat()
        # The Variety is spelled `DistributionRule` on a production-order line and
        # `CostingCode` on a document line -- same Dimension-1 profit centre, two
        # names. SAP's own disassembly orders carry it, so ours do too.
        variety = record.variety_code
        payload = {
            "ItemNo": record.item_code,
            "PlannedQuantity": float(record.quantity),
            "PostingDate": date_text,
            "DueDate": date_text,
            "StartDate": date_text,
            "Warehouse": record.warehouse_code,
            "Remarks": self._sap_comment(record),
            "ProductionOrderLines": [
                {
                    "ItemNo": component.item_code,
                    # Per piece and in total. SAP stores both (WOR1."BaseQty" and
                    # "PlannedQty"), and sending only the total would leave the
                    # recipe unstated on the order.
                    "BaseQuantity": float(component.qty_per_piece),
                    "PlannedQuantity": float(component.quantity),
                    "Warehouse": component.warehouse_code or record.warehouse_code,
                    "DistributionRule": variety,
                }
                for component in recovered
            ],
        }
        result = client.create_disassembly_order(payload)
        record.sap_order_doc_entry = result.get("DocEntry")
        record.sap_order_doc_num = str(result.get("DocNum") or "")
        record.save(
            update_fields=[
                "sap_order_doc_entry",
                "sap_order_doc_num",
                "variety_code",
                "posting_date",
                "updated_at",
            ]
        )
        self._map_line_numbers(client, record, recovered)

    @staticmethod
    def _map_line_numbers(client, record, recovered) -> None:
        """Record SAP's own line number for each component.

        The receipt has to name each line's ``BaseLine``, and that is the number
        SAP gave it on the order — not the position the app sent. Matched by item
        code, which is unique on a dismantle's components by constraint.
        """
        try:
            sap_lines = client.disassembly_order_lines(record.sap_order_doc_entry)
        except Exception as exc:
            logger.warning(
                "Could not read back the lines of disassembly order %s: %s",
                record.sap_order_doc_num,
                exc,
            )
            return

        numbers = {}
        for index, line in enumerate(sap_lines):
            item = line.get("ItemNo") or line.get("ItemCode")
            if item:
                number = line.get("LineNumber")
                numbers[item] = index if number is None else number

        changed = []
        for component in recovered:
            if component.item_code in numbers:
                component.sap_line_num = numbers[component.item_code]
                changed.append(component)
        if changed:
            DismantleComponent.objects.bulk_update(changed, ["sap_line_num"])

    def _post_receipt(self, client, record, recovered, posting_date, variety, user) -> None:
        """Document 2: Receipt from Production — the components come back."""
        if record.sap_receipt_doc_entry:
            return

        lines = []
        for component in recovered:
            line = {
                "ItemCode": component.item_code,
                "Quantity": float(component.quantity),
                "WarehouseCode": component.warehouse_code or record.warehouse_code,
                "BaseType": PRODUCTION_ORDER_OBJECT_TYPE,
                "BaseEntry": record.sap_order_doc_entry,
                "CostingCode": variety,
            }
            if component.sap_line_num is not None:
                line["BaseLine"] = component.sap_line_num
            if component.is_batch_managed:
                line["BatchNumbers"] = [
                    {
                        "BatchNumber": component.batch_number,
                        "Quantity": float(component.quantity),
                    }
                ]
            lines.append(line)

        result = client.create_production_receipt(
            {
                "DocDate": posting_date.isoformat(),
                "Comments": self._sap_comment(record),
                "DocumentLines": lines,
            }
        )
        record.sap_receipt_doc_entry = result.get("DocEntry")
        record.sap_receipt_doc_num = str(result.get("DocNum") or "")
        record.save(
            update_fields=[
                "sap_receipt_doc_entry",
                "sap_receipt_doc_num",
                "updated_at",
            ]
        )

    def _post_issue(self, client, record, posting_date, variety, user) -> None:
        """Document 3: Issue for Production — the parent is consumed.

        No ``BaseLine``: the parent is the order's header, not one of its
        component lines, which is why SAP's own document leaves it null.
        """
        if record.sap_issue_doc_entry:
            return

        line = {
            "ItemCode": record.item_code,
            "Quantity": float(record.quantity),
            "WarehouseCode": record.warehouse_code,
            "BaseType": PRODUCTION_ORDER_OBJECT_TYPE,
            "BaseEntry": record.sap_order_doc_entry,
            # Mandatory on every goods issue line (error 60003).
            "CostingCode": variety,
        }
        if record.batch_number:
            line["BatchNumbers"] = [
                {
                    "BatchNumber": record.batch_number,
                    "Quantity": float(record.quantity),
                }
            ]

        result = client.create_production_issue(
            {
                "DocDate": posting_date.isoformat(),
                "Comments": self._sap_comment(record),
                "DocumentLines": [line],
            }
        )
        record.sap_issue_doc_entry = result.get("DocEntry")
        record.sap_issue_doc_num = str(result.get("DocNum") or "")
        record.save(
            update_fields=["sap_issue_doc_entry", "sap_issue_doc_num", "updated_at"]
        )

    # -- shared preparation ---------------------------------------------------

    def _run_guards(self, record, variety, available, client) -> list:
        """Every check, in the order the operator can act on them."""
        guards.check_warehouse(record.warehouse_code)
        guards.check_quantity(record.quantity, available, batch=record.batch_number)
        guards.check_variety(record.item_code, variety)

        component_dicts = [
            {
                "item_code": component.item_code,
                "quantity": component.quantity,
                "recovered": component.recovered,
                "batch_number": component.batch_number,
                "is_batch_managed": component.is_batch_managed,
            }
            for component in record.active_components
        ]
        guards.check_components(component_dicts)

        recovered = record.recovered_components
        # Only checked for components not yet in SAP: a retry re-uses the batch
        # numbers the first run already received, and they exist by then.
        if not record.sap_receipt_doc_entry:
            batched = [
                c
                for c in component_dicts
                if c.get("recovered", True) and c.get("is_batch_managed")
            ]
            existing = client.existing_batches(
                [c["item_code"] for c in batched],
                [c["batch_number"] for c in batched if c["batch_number"]],
            )
            guards.check_component_batches(batched, existing)
        return recovered

    @staticmethod
    def _variety_for(client, record) -> str:
        """The Dimension-1 profit centre SAP wants on the goods issue.

        Resolved from the parent item, which is what the issue line is for. The
        same code is put on the receipt lines, matching what SAP's own screen
        writes — components are received against the parent's variety, not each
        of their own.
        """
        codes = client.return_variety_codes([record.item_code])
        return codes.get(record.item_code, "")

    @staticmethod
    def _available_quantity(client, record):
        """What the warehouse actually holds of this item, in this batch.

        Batch-level where SAP batch-manages the item, because that is the level
        the goods issue allocates at: a warehouse holding plenty across five
        batches still cannot issue more than one batch has.
        """
        if record.batch_number:
            for batch in client.available_batches(
                record.item_code, record.warehouse_code
            ):
                if batch.get("batch_number") == record.batch_number:
                    return _decimal(batch.get("quantity"))
            return Decimal(0)

        stock = client.dismantlable_stock(
            record.warehouse_code, search=record.item_code, limit=50
        )
        for row in stock:
            if row["item_code"] == record.item_code:
                return _decimal(row["on_hand"])
        return Decimal(0)

    @staticmethod
    def _sap_comment(record) -> str:
        """What the three documents say they are, in SAP's own comment field.

        SAP keeps no link from a disassembly order back to the return, so this
        text is the only place the connection is visible to someone reading the
        document in SAP rather than in the app.
        """
        text = f"Dismantle {record.entry_no}"
        if record.goods_return_id:
            text += f" of goods return {record.goods_return.entry_no}"
            if record.goods_return.customer_name:
                text += f" ({record.goods_return.customer_name})"
        if record.batch_number:
            text += f", batch {record.batch_number}"
        return text[:254]
