"""The five steps of a production order entry, as postings the SAP queue sends.

A try (``send``) is safe to repeat. Before writing, each step asks SAP whether
it is already done: the order by the entry's reference in its Comments, the
release and close by the order's status, the issue and receipt by the order
they point at (SAP allows one of each). What SAP already holds is recorded
instead of being posted twice.

Every step logs into SAP as the person who asked for it
(:mod:`production_orders.identity`), so it is that person SAP records, and
SAP's own per-user rules apply. A refused login is a refusal, not an outage.
"""

import logging

from django.db import transaction
from django.utils import timezone
from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.batch_stock_reader import HanaBatchStockReader
from sap_postings.services import Outcome

from . import identity, payloads
from .constants import (
    OBJECT_ISSUE_FOR_PRODUCTION,
    OBJECT_PRODUCTION_ORDER,
    OBJECT_RECEIPT_FROM_PRODUCTION,
    WIP_CARD_CODE,
)
from .models import STATUS_AFTER, ProductionOrderEntry, ProductionOrderEntryBatch, Step
from .services import EntryError, cap_refusal, q6, reader_for, resolve_series, stock_caps

logger = logging.getLogger(__name__)

#: Who-and-when fields each step fills.
_ACTOR_FIELDS = {
    Step.PLAN: ("planned_by", "planned_at"),
    Step.RELEASE: ("released_by", "released_at"),
    Step.ISSUE: ("issued_by", "issued_at"),
    Step.RECEIPT: ("received_by", "received_at"),
    Step.CLOSE: ("closed_by", "closed_at"),
}


class StepRefused(Exception):
    """The app's own check, made at send time, says SAP would refuse this."""


class _StepHandler:
    step = None

    @property
    def kind(self):
        from .services import KINDS

        return KINDS[self.step]

    def send(self, posting):
        entry = (
            ProductionOrderEntry.objects.select_related("company")
            .filter(pk=posting.source_id, is_active=True)
            .first()
        )
        if entry is None:
            return Outcome.rejected("The production entry no longer exists in the app.")
        if entry.step_done(self.step):
            return Outcome.posted(f"{Step(self.step).label} was already done.", result=_result(entry))
        if entry.next_step != self.step:
            return Outcome.rejected(f"The {Step(entry.next_step).label.lower()} step has to be posted first.")

        user = posting.created_by
        detail = {"step": self.step, "sap_user": identity.sap_user_code(user, entry.company) or ""}
        try:
            client = identity.sap_client_for(user, entry.company)
            done = self.run(entry, reader_for(entry.company), client, detail)
        except identity.NoSapLogin as exc:
            return Outcome.rejected(str(exc), detail=detail)
        except (StepRefused, EntryError) as exc:
            return Outcome.rejected(str(exc), detail=detail)
        except SAPValidationError as exc:
            return Outcome.rejected(f"SAP refused it: {exc}", detail=detail)
        except (SAPConnectionError, dbapi.Error) as exc:
            return Outcome.waiting(f"SAP could not be reached ({exc}).", detail=detail)
        except SAPDataError as exc:
            return Outcome.rejected(str(exc), detail=detail)

        entry = self._record(entry, user, done)
        return Outcome.posted(
            done.get("message") or f"{Step(self.step).label} posted.", result=_result(entry), detail=detail
        )

    def run(self, entry, reader, client, detail) -> dict:
        raise NotImplementedError

    def _record(self, entry, user, done) -> ProductionOrderEntry:
        """Store what SAP now holds and move the entry on, as one change."""
        with transaction.atomic():
            entry = ProductionOrderEntry.objects.select_for_update().get(pk=entry.pk)
            if entry.step_done(self.step):
                return entry
            for field, value in done.get("fields", {}).items():
                setattr(entry, field, value)
            entry.status = STATUS_AFTER[self.step]
            by, at = _ACTOR_FIELDS[self.step]
            setattr(entry, by, user)
            setattr(entry, at, timezone.now())
            entry.save()
            for line_id, values in done.get("lines", {}).items():
                entry.lines.filter(pk=line_id).update(**values)
            for line_id, batches in done.get("batches", {}).items():
                ProductionOrderEntryBatch.objects.filter(line_id=line_id).delete()
                ProductionOrderEntryBatch.objects.bulk_create(
                    ProductionOrderEntryBatch(
                        line_id=line_id, batch_number=b["BatchNumber"], quantity=q6(b["Quantity"])
                    )
                    for b in batches
                )
        return entry


def _result(entry) -> dict:
    return {
        "entry_no": entry.entry_no,
        "order": entry.sap_order_num,
        "issue": entry.sap_issue_num,
        "receipt": entry.sap_receipt_num,
    }


def _order(reader, entry) -> dict:
    order = reader.order(entry.sap_order_entry)
    if order is None:
        raise StepRefused(f"Production order {entry.sap_order_num} is no longer in SAP.")
    return order


def _require_status(order, status, what) -> None:
    if order["status"] != status:
        names = {"P": "planned", "R": "released", "L": "closed", "C": "cancelled"}
        raise StepRefused(
            f"Production order {order['doc_num']} is {names.get(order['status'], order['status'])} "
            f"in SAP; it has to be {names[status]} to {what}."
        )


def _not_future(day, what):
    if day > timezone.localdate():
        raise StepRefused(f"The {what} is in the future; SAP refuses it.")


def _series(entry, object_code, day) -> int:
    return resolve_series(entry.company, object_code, day)["series"]


class PlanHandler(_StepHandler):
    """Create the order, planned, with the BOM's lines.

    SAP takes a new production order only as planned (Service Layer -10), and
    every live order starts so: planned with its BOM lines, released half a
    minute later.
    """

    step = Step.PLAN

    def run(self, entry, reader, client, detail):
        found = reader.order_by_reference(entry.item_code, entry.sap_reference)
        if found:
            detail["outcome"] = "already in SAP"
            return self._done(entry, reader, found["doc_entry"], adopted=True)

        _not_future(entry.posting_date, "posting date")
        self._check_bom(entry)
        refusal = cap_refusal(
            stock_caps(entry.company, reader, entry_litres=q6(entry.quantity * (entry.litres_per_piece or 0))),
            on="order",
        )
        if refusal:
            raise StepRefused(refusal)

        payload = payloads.order_payload(
            entry,
            series=_series(entry, OBJECT_PRODUCTION_ORDER, entry.posting_date),
            card_code=WIP_CARD_CODE.get(entry.company.code, ""),
        )
        detail["payload"] = payload
        answer = client.post("ProductionOrders", payload, label="create the production order")
        doc_entry = answer.get("AbsoluteEntry") or answer.get("DocEntry")
        if not doc_entry:
            # Committed, but the answer does not say where: the next try finds
            # it by the reference instead of posting again.
            raise SAPConnectionError("SAP did not say which order it created.")
        return self._done(entry, reader, int(doc_entry), adopted=False)

    def _check_bom(self, entry):
        """SAP refuses a standard order that differs from the BOM; the BOM may
        have changed since the entry was saved."""
        from sap_client.hana.bom_reader import HanaBOMReader

        tree = HanaBOMReader(CompanyContext(entry.company.code)).get_tree(entry.item_code)
        current = [
            (line["item_code"], q6(q6(line["quantity"]) / q6(tree["quantity"])))
            for line in (tree or {}).get("lines", [])
            if line["item_type"] in ("item", "resource")
        ]
        saved = [(line.item_code, q6(line.base_quantity)) for line in entry.lines.all()]
        if current != saved:
            raise StepRefused(
                f"{entry.item_code}'s BOM in SAP has changed since this entry was saved. "
                "Open the Plan step and save it again to take the new BOM."
            )

    def _done(self, entry, reader, doc_entry, *, adopted):
        order = reader.order(doc_entry)
        if order is None:
            raise SAPConnectionError(f"Order {doc_entry} could not be read back from SAP yet.")
        sap_lines = sorted(order["lines"], key=lambda l: (l["visual_order"] or 0, l["line_num"]))
        entry_lines = list(entry.lines.all())
        if [l["item_code"] for l in sap_lines] != [l.item_code for l in entry_lines]:
            raise StepRefused(
                f"Production order {order['doc_num']} in SAP does not have this entry's lines; "
                "check it in SAP."
            )
        return {
            "fields": {"sap_order_entry": doc_entry, "sap_order_num": order["doc_num"]},
            "lines": {
                line.pk: {"sap_line_num": sap["line_num"], "planned_quantity": q6(sap["planned_quantity"])}
                for line, sap in zip(entry_lines, sap_lines)
            },
            "message": (
                f"Production order {order['doc_num']}"
                + (" was already in SAP (not posted twice)." if adopted else " created, planned.")
            ),
        }


class ReleaseHandler(_StepHandler):
    """Release the planned order."""

    step = Step.RELEASE

    def run(self, entry, reader, client, detail):
        order = _order(reader, entry)
        if order["status"] == "R":
            detail["outcome"] = "already released in SAP"
            return {"message": f"Production order {order['doc_num']} was already released in SAP."}
        _require_status(order, "P", "release it")
        refusal = cap_refusal(stock_caps(entry.company, reader), on="order")
        if refusal:
            raise StepRefused(refusal)
        payload = payloads.release_payload()
        detail["payload"] = payload
        client.patch(
            f"ProductionOrders({int(entry.sap_order_entry)})",
            payload,
            label=f"release production order {order['doc_num']}",
        )
        return {"message": f"Production order {order['doc_num']} released."}


class IssueHandler(_StepHandler):
    """Issue every line at the order's planned quantity."""

    step = Step.ISSUE

    def run(self, entry, reader, client, detail):
        found = reader.issue_for(entry.sap_order_entry)
        if found:
            detail["outcome"] = "already in SAP"
            return self._done(found, {}, adopted=True)

        order = _order(reader, entry)
        _require_status(order, "R", "issue to it")
        if any(line["issued_quantity"] > 0 for line in order["lines"]):
            raise StepRefused(
                f"Something was issued to order {order['doc_num']} outside the app; check it in SAP."
            )
        doc_date = entry.issue_date or timezone.localdate()
        _not_future(doc_date, "issue date")
        planned = {line["line_num"]: q6(line["planned_quantity"]) for line in order["lines"]}
        batch_reader = HanaBatchStockReader(CompanyContext(entry.company.code))

        rows, picked = [], {}
        for line in entry.lines.prefetch_related("batches"):
            if line.sap_line_num is None or line.sap_line_num not in planned:
                raise StepRefused(f"{line.item_code} is not on order {order['doc_num']} in SAP.")
            quantity = planned[line.sap_line_num]
            batches = []
            if line.batch_managed:
                chosen = [
                    {"BatchNumber": b.batch_number, "Quantity": q6(b.quantity)} for b in line.batches.all()
                ]
                if chosen:
                    total = sum((b["Quantity"] for b in chosen), q6(0))
                    if total != quantity:
                        raise StepRefused(
                            f"{line.item_code}'s chosen batches add up to {total:f}; the order needs "
                            f"{quantity:f}. Choose its batches again on the Issue page."
                        )
                    batches = batch_reader.check_allocation(line.item_code, line.warehouse, chosen)
                else:
                    batches = batch_reader.allocate_fifo(line.item_code, line.warehouse, quantity)
                picked[line.pk] = batches
            rows.append({"line": line, "quantity": quantity, "batches": batches})

        payload = payloads.issue_payload(
            entry,
            series=_series(entry, OBJECT_ISSUE_FOR_PRODUCTION, doc_date),
            branch=reader.branch_of(rows[0]["line"].warehouse) if rows else None,
            lines=rows,
            doc_date=doc_date,
        )
        detail["payload"] = payload
        answer = client.post("InventoryGenExits", payload, label="issue for production")
        if not answer.get("DocEntry"):
            raise SAPConnectionError("SAP did not say which issue it created.")
        return self._done(
            {"doc_entry": int(answer["DocEntry"]), "doc_num": int(answer["DocNum"])}, picked,
            adopted=False, doc_date=doc_date,
        )

    def _done(self, document, picked, *, adopted, doc_date=None):
        fields = {"sap_issue_entry": document["doc_entry"], "sap_issue_num": document["doc_num"]}
        if doc_date:
            fields["issue_date"] = doc_date
        return {
            "fields": fields,
            "batches": picked,
            "message": (
                f"Issue for production {document['doc_num']}"
                + (" was already in SAP (not posted twice)." if adopted else " posted.")
            ),
        }


class ReceiptHandler(_StepHandler):
    """Receive the finished goods under the entry's batch."""

    step = Step.RECEIPT

    def run(self, entry, reader, client, detail):
        found = reader.receipt_for(entry.sap_order_entry)
        if found:
            detail["outcome"] = "already in SAP"
            return self._done(found, adopted=True)

        _require_status(_order(reader, entry), "R", "receive from it")
        if not reader.issue_for(entry.sap_order_entry):
            raise StepRefused("SAP refuses a receipt before the materials are issued.")
        if not (entry.batch_number and entry.mfg_date and entry.expiry_date):
            raise StepRefused("The batch is not filled in on the Receipt page.")
        doc_date = entry.receipt_date or timezone.localdate()
        _not_future(doc_date, "receipt date")
        batch_managed = reader.batch_managed_flags([entry.item_code]).get(entry.item_code, False)
        if batch_managed and reader.batch_exists(entry.item_code, entry.batch_number):
            raise StepRefused(
                f"Batch {entry.batch_number} already exists in SAP for {entry.item_code}. "
                "Change the batch's last two digits on the Receipt page."
            )
        refusal = cap_refusal(stock_caps(entry.company, reader), on="receipt")
        if refusal:
            raise StepRefused(refusal)

        payload = payloads.receipt_payload(
            entry,
            series=_series(entry, OBJECT_RECEIPT_FROM_PRODUCTION, doc_date),
            branch=reader.branch_of(entry.warehouse),
            batch_managed=batch_managed,
            doc_date=doc_date,
        )
        detail["payload"] = payload
        answer = client.post("InventoryGenEntries", payload, label="receipt from production")
        if not answer.get("DocEntry"):
            raise SAPConnectionError("SAP did not say which receipt it created.")
        return self._done(
            {"doc_entry": int(answer["DocEntry"]), "doc_num": int(answer["DocNum"])}, adopted=False,
            doc_date=doc_date,
        )

    def _done(self, document, *, adopted, doc_date=None):
        fields = {"sap_receipt_entry": document["doc_entry"], "sap_receipt_num": document["doc_num"]}
        if doc_date:
            fields["receipt_date"] = doc_date
        return {
            "fields": fields,
            "message": (
                f"Receipt from production {document['doc_num']}"
                + (" was already in SAP (not posted twice)." if adopted else " posted.")
            ),
        }


class CloseHandler(_StepHandler):
    """Close the order once it has its issue and its receipt."""

    step = Step.CLOSE

    def run(self, entry, reader, client, detail):
        order = _order(reader, entry)
        if order["status"] == "L":
            detail["outcome"] = "already closed in SAP"
            return {"message": f"Production order {order['doc_num']} was already closed in SAP."}
        _require_status(order, "R", "close it")
        if not reader.issue_for(entry.sap_order_entry) or not reader.receipt_for(entry.sap_order_entry):
            raise StepRefused("SAP closes an order only once it has both its issue and its receipt.")
        closing_date = entry.close_date or timezone.localdate()
        _not_future(closing_date, "closing date")
        refusal = cap_refusal(stock_caps(entry.company, reader), on="order")
        if refusal:
            raise StepRefused(refusal)
        payload = payloads.close_payload(closing_date)
        detail["payload"] = payload
        client.patch(
            f"ProductionOrders({int(entry.sap_order_entry)})",
            payload,
            label=f"close production order {order['doc_num']}",
        )
        return {
            "fields": {"close_date": closing_date},
            "message": f"Production order {order['doc_num']} closed.",
        }


# ---------------------------------------------------------------------------
# Changes to an order already in SAP (not steps)
# ---------------------------------------------------------------------------


class _ChangeHandler:
    """A change to the order in SAP, as the person who asked for it."""

    kind = ""

    def send(self, posting):
        entry = (
            ProductionOrderEntry.objects.select_related("company")
            .filter(pk=posting.source_id, is_active=True)
            .first()
        )
        if entry is None:
            return Outcome.rejected("The production entry no longer exists in the app.")
        user = posting.created_by
        detail = {"change": self.kind, "sap_user": identity.sap_user_code(user, entry.company) or ""}
        try:
            client = identity.sap_client_for(user, entry.company)
            message = self.run(entry, posting, reader_for(entry.company), client, detail)
        except identity.NoSapLogin as exc:
            return Outcome.rejected(str(exc), detail=detail)
        except (StepRefused, EntryError) as exc:
            return Outcome.rejected(str(exc), detail=detail)
        except SAPValidationError as exc:
            return Outcome.rejected(f"SAP refused it: {exc}", detail=detail)
        except (SAPConnectionError, dbapi.Error) as exc:
            return Outcome.waiting(f"SAP could not be reached ({exc}).", detail=detail)
        except SAPDataError as exc:
            return Outcome.rejected(str(exc), detail=detail)
        return Outcome.posted(message, result=_result(entry), detail=detail)

    def run(self, entry, posting, reader, client, detail) -> str:
        raise NotImplementedError


class ReplanHandler(_ChangeHandler):
    """A new product, quantity, date or remarks for an order still planned:
    the order is patched, its lines replaced by the new product's BOM."""

    kind = "production_order.replan"

    def run(self, entry, posting, reader, client, detail):
        from datetime import date as _date

        from .services import _write_lines, plan_preview

        if entry.status != "PLANNED":
            raise StepRefused("Only an order that is planned can be changed; this one has moved on.")
        order = _order(reader, entry)
        _require_status(order, "P", "change it")
        params = dict(posting.params)
        params["posting_date"] = _date.fromisoformat(params["posting_date"])
        built = plan_preview(entry.company, params)
        already = (
            order["planned_quantity"] == q6(built["quantity"])
            and [line["item_code"] for line in sorted(order["lines"], key=lambda l: l["line_num"])]
            == [line["item_code"] for line in built["lines"]]
            and reader.order_by_reference(built["item_code"], entry.sap_reference)
        )
        if already:
            detail["outcome"] = "already in SAP"
        else:
            refusal = cap_refusal(built["caps"], on="order")
            if refusal:
                raise StepRefused(refusal)
            payload = payloads.replan_payload(built, reference=entry.sap_reference)
            detail["payload"] = payload
            client.patch(
                f"ProductionOrders({int(entry.sap_order_entry)})",
                payload,
                headers=payloads.REPLACE_COLLECTIONS,
                label=f"change planned order {order['doc_num']}",
            )
        order = reader.order(entry.sap_order_entry)
        if order is None:
            raise SAPConnectionError("The changed order could not be read back from SAP yet.")
        sap_lines = sorted(order["lines"], key=lambda l: (l["visual_order"] or 0, l["line_num"]))
        if [l["item_code"] for l in sap_lines] != [l["item_code"] for l in built["lines"]]:
            raise StepRefused(
                f"Production order {order['doc_num']} in SAP does not have the new BOM's lines; "
                "check it in SAP."
            )
        with transaction.atomic():
            entry = ProductionOrderEntry.objects.select_for_update().get(pk=entry.pk)
            product_changed = built["item_code"] != entry.item_code
            for field in (
                "item_code", "item_name", "uom", "pieces_per_box", "litres_per_piece", "boxes",
                "loose_pieces", "quantity", "warehouse", "bom_quantity", "posting_date", "remarks",
            ):
                setattr(entry, field, built[field])
            if product_changed:
                # The variety and the batch belong to the product.
                entry.variety = built["variety"]
                entry.line_code, entry.oil_code, entry.batch_number = "", "", ""
                entry.batch_sequence = entry.mfg_date = entry.expiry_date = None
            entry.updated_by = posting.created_by
            entry.save()
            _write_lines(entry, built["lines"])
            for line, sap in zip(entry.lines.all(), sap_lines):
                line.sap_line_num = sap["line_num"]
                line.planned_quantity = q6(sap["planned_quantity"])
                line.save(update_fields=["sap_line_num", "planned_quantity"])
        return f"Production order {order['doc_num']} changed: {built['item_code']} × {q6(built['quantity']):g}."


class UnreleaseHandler(_ChangeHandler):
    """Take a released order back to planned, while nothing is issued to it."""

    kind = "production_order.unrelease"

    def run(self, entry, posting, reader, client, detail):
        if entry.status not in ("RELEASED", "PLANNED"):
            raise StepRefused("Materials are already issued to this order; it can no longer go back to planned.")
        order = _order(reader, entry)
        if order["status"] == "R":
            if reader.issue_for(entry.sap_order_entry) or any(
                line["issued_quantity"] > 0 for line in order["lines"]
            ):
                raise StepRefused(
                    f"Something is issued to order {order['doc_num']}; SAP cannot take it back to planned."
                )
            payload = payloads.unrelease_payload()
            detail["payload"] = payload
            client.patch(
                f"ProductionOrders({int(entry.sap_order_entry)})",
                payload,
                label=f"take production order {order['doc_num']} back to planned",
            )
        elif order["status"] == "P":
            detail["outcome"] = "already planned in SAP"
        else:
            _require_status(order, "R", "take it back to planned")
        with transaction.atomic():
            entry = ProductionOrderEntry.objects.select_for_update().get(pk=entry.pk)
            entry.status = "PLANNED"
            entry.released_by = None
            entry.released_at = None
            entry.updated_by = posting.created_by
            entry.save()
        return f"Production order {order['doc_num']} is planned again; change it, then release it."
