"""Company-scoped orchestration for Short Dispatch.

Business errors raise ``ValueError`` (views translate to HTTP 400); cross-company
access violations raise DRF ``PermissionDenied`` (403). The invoice is read from
SAP on demand and never stored beyond its doc-entry plus a per-line snapshot.

The SAP rules an A/R Return has to satisfy live in ``goods_return.guards`` and are
imported rather than copied. Every one of them was read out of
``SBO_SP_TRANSACTIONNOTIFICATION`` or established by posting into the sandbox, and
they belong to the *document*, not to the module that posts it -- a short dispatch
and a customer return are refused by SAP for exactly the same reasons. One copy
means the SAP team's next rule change lands in one place.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from company.models import Company

from .models import ShortDispatch, ShortDispatchItem

logger = logging.getLogger(__name__)

# SAP object code for an A/R invoice -- the BaseType its batch allocations carry
# in IBT1.
BASE_TYPE_AR_INVOICE = 13


class ShortDispatchService:
    """Bound to the active-header company for lookups and creation.

    Reads of an existing entry resolve the company from the *record* and enforce
    that it is one of the caller's (``allowed_company_ids``) -- the cross-company
    security boundary.
    """

    def __init__(self, company: Company | None = None):
        self.company = company

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _client(company: Company):
        # Imported lazily: sap_client pulls in the HANA driver stack.
        from sap_client.client import SAPClient

        return SAPClient(company_code=company.code)

    @staticmethod
    def _bill(company: Company, invoice_number: str) -> dict:
        from dispatch_plans.services import DispatchPlansService

        number = (invoice_number or "").strip()
        if not number:
            raise ValueError("Enter the invoice number.")
        try:
            bill = DispatchPlansService(company.code).get_bill_by_number(number)
        except Exception as exc:  # a SAP/HANA read failure is a clean 400, not a 500
            logger.warning("Short dispatch invoice lookup failed for %s: %s", number, exc)
            raise ValueError(f"Could not look up invoice {number}.")
        if not bill:
            raise ValueError(f"No SAP invoice found for {number}.")
        return bill

    def _require_company(self) -> Company:
        if self.company is None:
            raise ValueError("A company context is required.")
        return self.company

    def _get_scoped(self, pk, allowed_company_ids) -> ShortDispatch:
        entry = (
            ShortDispatch.objects.filter(pk=pk, is_active=True)
            .select_related("company", "posted_by")
            .prefetch_related("lines")
            .first()
        )
        if entry is None:
            raise ValueError("Short dispatch not found.")
        if entry.company_id not in allowed_company_ids:
            raise PermissionDenied("This record belongs to a company you cannot access.")
        return entry

    # -- reads -----------------------------------------------------------------

    def list_entries(self, company_ids, *, search=None, from_date=None, to_date=None):
        qs = (
            ShortDispatch.objects.filter(is_active=True, company_id__in=company_ids)
            .select_related("company", "posted_by")
            .prefetch_related("lines")
        )
        if from_date:
            qs = qs.filter(created_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(created_at__date__lte=to_date)
        if search:
            # The invoice number first: nobody looks a short dispatch up by its
            # own number, they look it up by the bill it corrected.
            qs = qs.filter(
                Q(sap_invoice_doc_num__icontains=search)
                | Q(entry_no__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(customer_code__icontains=search)
                | Q(sap_return_doc_num__icontains=search)
                | Q(lines__item_code__icontains=search)
            ).distinct()
        return qs

    def get_entry(self, pk, allowed_company_ids) -> ShortDispatch:
        return self._get_scoped(pk, allowed_company_ids)

    def list_warehouses(self):
        """Every active warehouse, not only the goods-return ones.

        Short stock goes back into the warehouse it was billed out of -- it never
        left -- so the ``-GR`` list a customer return picks from is the wrong one
        here.
        """
        company = self._require_company()
        return self._client(company).get_active_warehouses()

    def lookup_invoice(self, invoice_number: str) -> dict:
        """The invoice as the form needs it: lines, batches, and what is left.

        Three things are added to SAP's own lines:

        * ``original_batch_number`` -- the batch SAP actually allocated on that
          line (IBT1). The floor is standing in front of it, and the posted return
          cannot reuse it, so it is shown and recorded rather than re-keyed.
        * ``already_short`` -- how much of the line an earlier short dispatch
          already put back, so a second entry against the same bill cannot return
          more than was billed.
        * ``remaining_quantity`` -- the cap the form enforces.
        """
        company = self._require_company()
        bill = self._bill(company, invoice_number)
        doc_entry = bill["doc_entry"]

        batches = self._invoice_batches(company, doc_entry)
        already = self._already_short(company, doc_entry)

        lines = []
        for line in bill.get("items", []):
            line_num = line.get("line_num")
            billed = Decimal(str(line.get("quantity") or 0))
            done = already.get((line_num, line.get("item_code") or ""), Decimal("0"))
            lines.append(
                {
                    "line_num": line_num,
                    "item_code": line.get("item_code") or "",
                    "item_name": line.get("item_name") or "",
                    "uom": line.get("uom") or "",
                    "quantity": float(billed),
                    "rate": float(line.get("rate") or 0),
                    "tax_code": line.get("tax_code") or "",
                    "warehouse_code": line.get("warehouse_code") or "",
                    "original_batch_number": batches.get(line_num, ""),
                    "already_short": float(done),
                    "remaining_quantity": float(max(billed - done, Decimal("0"))),
                }
            )

        return {
            "doc_entry": doc_entry,
            "doc_num": bill.get("doc_num") or "",
            "doc_date": bill.get("doc_date"),
            "card_code": bill.get("card_code") or "",
            "card_name": bill.get("card_name") or "",
            # What the form preselects: the warehouse most of the bill was picked
            # from. A bill split across floors still posts into one warehouse, so
            # the operator is shown the per-line source and can change it.
            "default_warehouse_code": self._dominant_warehouse(lines),
            "lines": lines,
            # Earlier entries against this bill. Not a block -- a second shortfall
            # on one invoice is possible -- but the operator should see it before
            # they post another document.
            "existing_entries": [
                {
                    "id": entry.id,
                    "entry_no": entry.entry_no,
                    "sap_return_doc_num": entry.sap_return_doc_num,
                    "created_at": entry.created_at,
                }
                for entry in ShortDispatch.objects.filter(
                    is_active=True, company=company, sap_invoice_doc_entry=doc_entry
                ).order_by("id")
            ],
        }

    def _invoice_batches(self, company: Company, doc_entry: int) -> dict:
        """``INV1.LineNum -> batch number`` for what the invoice issued.

        Read from IBT1 rather than the Service Layer: a ``GET`` on a document
        comes back with an empty ``BatchNumbers`` even for documents that carry
        them. A line split across two batches keeps the first -- the field is a
        note of what is on the floor, not an allocation the return has to match,
        since SAP makes the return mint its own batch anyway.

        Never fatal: an item without batch management simply has no rows here, and
        the form works without them.
        """
        try:
            rows = self._client(company).posted_batch_allocations(
                doc_entry, base_type=BASE_TYPE_AR_INVOICE
            )
        except Exception as exc:
            logger.warning("Could not read invoice batches for %s: %s", doc_entry, exc)
            return {}
        batches: dict = {}
        for row in rows:
            if not row.get("is_issue"):
                continue
            line_num = row.get("line_num")
            if line_num is None or line_num in batches:
                continue
            if row.get("batch_number"):
                batches[line_num] = row["batch_number"]
        return batches

    @staticmethod
    def _already_short(company: Company, doc_entry: int) -> dict:
        """``(line_num, item_code) -> quantity`` already returned for this bill."""
        rows = ShortDispatchItem.objects.filter(
            is_active=True,
            short_dispatch__is_active=True,
            short_dispatch__company=company,
            short_dispatch__sap_invoice_doc_entry=doc_entry,
        ).values_list("source_line_num", "item_code", "short_quantity")
        totals: dict = {}
        for line_num, item_code, quantity in rows:
            key = (line_num, item_code or "")
            totals[key] = totals.get(key, Decimal("0")) + (quantity or Decimal("0"))
        return totals

    @staticmethod
    def _dominant_warehouse(lines) -> str:
        """The warehouse most of the bill's lines were billed out of."""
        counts: dict = {}
        for line in lines:
            code = line.get("warehouse_code") or ""
            if code:
                counts[code] = counts.get(code, 0) + 1
        if not counts:
            return ""
        return max(counts.items(), key=lambda pair: pair[1])[0]

    # -- create + post ---------------------------------------------------------

    @transaction.atomic
    def create_and_post(self, data, user) -> ShortDispatch:
        """The whole form, in one call: record the shortfall and post the Return.

        There is no draft stage on purpose. A short dispatch is keyed in one
        sitting by the person who has just found the stock still standing there,
        and a half-saved one would be a record of a correction nobody made. So
        either SAP takes the document and the entry exists, or SAP refuses it, the
        transaction rolls back, and the operator is told why with the form still in
        front of them.

        SAP is called last, after every database write and every guard, because a
        posted A/R Return cannot be withdrawn by this app (SAP restricts cancelling
        one to a named list of users; a live ``Cancel`` came back ``-1116``).
        Anything that can be refused is refused before then.
        """
        company = self._require_company()

        bill = self._bill(company, data.get("invoice_number") or "")
        doc_entry = bill["doc_entry"]
        customer_code = (bill.get("card_code") or "").strip()
        if not customer_code:
            raise ValueError(
                f"Invoice {bill.get('doc_num')} has no customer on it in SAP, so a "
                f"return cannot be posted against it."
            )

        warehouse_code = (data.get("warehouse_code") or "").strip()
        if not warehouse_code:
            raise ValueError("Select the warehouse the stock goes back into.")

        lines = self._prepare_lines(company, bill, data.get("lines") or [])

        entry = ShortDispatch.objects.create(
            company=company,
            entry_no=ShortDispatch.generate_entry_no(),
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=bill.get("doc_num") or "",
            customer_code=customer_code,
            customer_name=(bill.get("card_name") or "").strip(),
            warehouse_code=warehouse_code,
            remarks=(data.get("remarks") or "").strip(),
            created_by=user,
        )
        for line in lines:
            ShortDispatchItem.objects.create(short_dispatch=entry, created_by=user, **line)

        self._post_return(entry, user)
        return entry

    def _prepare_lines(self, company: Company, bill: dict, raw_lines) -> list[dict]:
        """The submitted lines, checked against the invoice they claim to be on.

        The invoice is the authority for everything except the short quantity and
        the reason: item, unit, price, tax code and source warehouse are taken from
        the bill rather than from the request, so a stale or tampered form cannot
        put an item on a return that was never sold.
        """
        by_line = {line.get("line_num"): line for line in bill.get("items", [])}
        already = self._already_short(company, bill["doc_entry"])
        batches = self._invoice_batches(company, bill["doc_entry"])

        prepared: list[dict] = []
        seen: set[str] = set()
        for raw in raw_lines:
            quantity = Decimal(str(raw.get("short_quantity") or 0))
            if quantity <= 0:
                continue  # this line went out in full

            source = by_line.get(raw.get("source_line_num"))
            if source is None:
                raise ValueError(
                    f"Line {raw.get('source_line_num')} is not on invoice "
                    f"{bill.get('doc_num')}."
                )

            item_code = source.get("item_code") or ""
            # SAP refuses duplicate item lines on one document (160020) and names
            # neither of them. Caught here, where the two rows are still in front
            # of us.
            if item_code in seen:
                raise ValueError(
                    f"{item_code} is on this invoice more than once. SAP needs the "
                    f"short quantities combined onto a single line (error 160020), "
                    f"so enter the whole shortfall against one of them."
                )
            seen.add(item_code)

            billed = Decimal(str(source.get("quantity") or 0))
            done = already.get((source.get("line_num"), item_code), Decimal("0"))
            remaining = billed - done
            if quantity > remaining:
                if done:
                    raise ValueError(
                        f"{item_code}: {done} of the {billed} billed has already been "
                        f"returned on an earlier short dispatch, so at most "
                        f"{remaining} is left to return."
                    )
                raise ValueError(
                    f"{item_code}: {quantity} cannot be returned against a line "
                    f"billed for {billed}."
                )

            prepared.append(
                {
                    "source_line_num": source.get("line_num"),
                    "item_code": item_code,
                    "item_name": source.get("item_name") or "",
                    "uom": source.get("uom") or "",
                    "invoice_quantity": billed,
                    "short_quantity": quantity,
                    "unit_price": Decimal(str(source.get("rate") or 0)),
                    "tax_code": source.get("tax_code") or "",
                    "source_warehouse_code": source.get("warehouse_code") or "",
                    "original_batch_number": batches.get(source.get("line_num"), ""),
                    "reason": raw.get("reason") or "SHORT",
                    "remarks": (raw.get("remarks") or "").strip()[:255],
                }
            )

        if not prepared:
            raise ValueError("Enter a short quantity for at least one item.")
        return prepared

    def _post_return(self, entry: ShortDispatch, user) -> None:
        """One standalone A/R Return for this entry's invoice.

        Standalone because SAP refuses a return based on an invoice outright
        (``'13' is not a valid value for property 'BaseType'``), which means the
        app has to supply what a copied line would have inherited: the Variety, the
        tax code and the return cost. All three are read from SAP; none is
        defaulted.
        """
        from sap_client.context import CompanyContext
        from sap_client.service_layer.returns_writer import ReturnsWriter

        from goods_return import guards

        client = self._client(entry.company)
        lines = list(entry.lines.all())
        item_codes = [line.item_code for line in lines]

        guards.check_posting_date(timezone.localdate())
        guards.check_customer(
            entry.customer_code, client.customer_group_code(entry.customer_code)
        )
        branch_id = client.warehouse_branch_id(entry.warehouse_code)
        guards.check_warehouse(entry.warehouse_code, branch_id)

        variety_codes = client.return_variety_codes(item_codes)
        return_costs = client.return_costs(item_codes, entry.warehouse_code)

        # The invoice line already recorded the code it was billed under; only ask
        # SAP for the ones the snapshot is missing.
        tax_codes = {line.item_code: line.tax_code for line in lines if line.tax_code}
        unknown = [line.item_code for line in lines if not line.tax_code]
        if unknown:
            tax_codes.update(client.return_tax_codes(entry.customer_code, unknown))

        addresses = self._place_of_supply(entry, client)
        interstate = guards.is_interstate(
            client.branch_state(branch_id), addresses.get("ship_state", "")
        )
        if interstate is not None:
            available = client.ar_tax_codes()
            tax_codes = {
                item: guards.align_tax_code(
                    code, interstate=interstate, available=available, item_code=item
                )
                for item, code in tax_codes.items()
            }

        guards.check_lines(
            [{"item_code": line.item_code, "quantity": line.short_quantity} for line in lines],
            variety_codes=variety_codes,
            tax_codes=tax_codes,
            return_costs=return_costs,
        )

        batch_managed = self._batch_managed(client, item_codes)
        payload = self._sap_payload(
            entry,
            lines,
            branch_id,
            addresses,
            variety_codes=variety_codes,
            tax_codes=tax_codes,
            return_costs=return_costs,
            batch_managed=batch_managed,
        )

        # Asked before posting, not only after a crash: the reference is unique to
        # this entry, so a document already carrying it *is* this one, and a second
        # copy of a return nobody can cancel is the one mistake worth a round-trip
        # to avoid.
        existing = client.find_goods_return_by_reference(
            entry.customer_code, payload["NumAtCard"]
        )
        if existing:
            logger.warning(
                "A/R Return %s already exists in SAP for %s; not posting again.",
                existing.get("doc_num"),
                entry.entry_no,
            )
            result = {"DocEntry": existing["doc_entry"], "DocNum": existing["doc_num"]}
        else:
            try:
                result = ReturnsWriter(CompanyContext(entry.company.code)).create(payload)
            except Exception as exc:
                logger.error(
                    "SAP A/R Return post failed for %s (invoice %s): %s",
                    entry.entry_no,
                    entry.sap_invoice_doc_num,
                    exc,
                )
                # Raised rather than recorded: nothing reached SAP, so the
                # transaction rolls back and no half-made entry is left behind.
                raise ValueError(f"SAP rejected the return note: {exc}")

        entry.sap_return_doc_entry = result.get("DocEntry")
        entry.sap_return_doc_num = str(result.get("DocNum") or "")
        entry.posted_at = timezone.now()
        entry.posted_by = user
        entry.updated_by = user
        entry.save(
            update_fields=[
                "sap_return_doc_entry",
                "sap_return_doc_num",
                "posted_at",
                "posted_by",
                "updated_by",
                "updated_at",
            ]
        )

    @staticmethod
    def _batch_managed(client, item_codes) -> dict:
        """Which items SAP requires a batch on.

        An unreadable answer is treated as batch-managed: finished goods
        overwhelmingly are, and leaving the batch off one that needs it fails the
        whole document (-4014).
        """
        try:
            return client.batch_managed_flags(item_codes)
        except Exception as exc:
            logger.warning("Could not read batch flags: %s", exc)
            return {}

    @staticmethod
    def _place_of_supply(entry: ShortDispatch, client) -> dict:
        """The ship-to / bill-to the document must carry, and its GST state.

        Read off the invoice being corrected. Leave them off and SAP resolves the
        place of supply from ``OCRD.ShipToDef``, which for a distributor holding
        stock in several states is usually not the state that was billed -- the
        return then reads as inter-state while carrying the invoice's CGST+SGST
        code, and SAP refuses the document outright (254000293).
        """
        addresses = client.invoice_addresses(entry.sap_invoice_doc_entry) or {}
        if not addresses.get("ship_to_code"):
            addresses = client.customer_last_invoice_addresses(entry.customer_code) or {}
        # `INV12` can be missing on an old document; the address itself still knows
        # its state.
        if addresses.get("ship_to_code") and not addresses.get("ship_state"):
            addresses["ship_state"] = client.customer_address_state(
                entry.customer_code, addresses["ship_to_code"]
            )
        return addresses

    def _sap_payload(
        self,
        entry: ShortDispatch,
        lines,
        branch_id,
        addresses,
        *,
        variety_codes,
        tax_codes,
        return_costs,
        batch_managed,
    ) -> dict:
        from goods_return import guards

        payload = {
            "CardCode": entry.customer_code,
            # A marketing document spells the branch differently from a stock
            # transfer; omitting this fails with -5002.
            "BPL_IDAssignedToInvoice": branch_id,
            "NumAtCard": guards.check_reference(self._reference(entry)),
            "Comments": (
                f"Short dispatch {entry.entry_no} against invoice "
                f"{entry.sap_invoice_doc_num or entry.sap_invoice_doc_entry}"
            )[:254],
            "DocumentLines": [
                self._sap_line(
                    entry,
                    line,
                    variety=variety_codes[line.item_code],
                    tax_code=tax_codes[line.item_code],
                    return_cost=return_costs[line.item_code],
                    batch_managed=batch_managed.get(line.item_code, True),
                )
                for line in lines
            ],
        }
        if addresses.get("ship_to_code"):
            payload["ShipToCode"] = addresses["ship_to_code"]
        if addresses.get("pay_to_code") or addresses.get("ship_to_code"):
            payload["PayToCode"] = addresses.get("pay_to_code") or addresses["ship_to_code"]
        return payload

    @staticmethod
    def _reference(entry: ShortDispatch) -> str:
        """The customer reference this document carries.

        It is the app's only handle on a document it has already posted, so it
        names both the entry and the bill it corrects -- and being unique per
        (entry, invoice) it is also what stops SAP taking a second copy when the
        same reference comes back (-5002).
        """
        invoice = entry.sap_invoice_doc_num or str(entry.sap_invoice_doc_entry)
        return f"{entry.entry_no} INV {invoice}"

    @staticmethod
    def _sap_line(entry: ShortDispatch, line, *, variety, tax_code, return_cost, batch_managed):
        from goods_return import guards

        notes = []
        if line.original_batch_number:
            notes.append(f"Billed batch {line.original_batch_number}")
        notes.append(line.get_reason_display())
        if line.remarks:
            notes.append(line.remarks)

        sap_line = {
            "ItemCode": line.item_code,
            "Quantity": float(line.short_quantity),
            "WarehouseCode": entry.warehouse_code,
            # Zero price: the stock comes back, the customer is not credited here.
            # The credit note against the invoice is a separate finance step.
            "UnitPrice": 0,
            "TaxCode": tax_code,
            "CostingCode": variety,
            # ReturnCost x Quantity becomes OINM.TransValue -- what the stock is
            # worth coming back in. Mandatory for batch-managed items (160021).
            "EnableReturnCost": "tYES",
            "ReturnCost": float(return_cost),
            "FreeText": " / ".join(notes)[:100],
        }
        if batch_managed:
            # The batch standing on the floor cannot be reused: SAP refuses a
            # return into a batch that already exists (10001226), even one held in
            # another warehouse. So a fresh number is minted from the line's own
            # id, and the real batch rides in FreeText above -- the only place it
            # survives.
            sap_line["BatchNumbers"] = [
                {
                    "BatchNumber": guards.batch_number_for(entry.entry_no, line.pk),
                    "Quantity": float(line.short_quantity),
                }
            ]
        return sap_line

    # -- the printed Return Note ----------------------------------------------

    def print_payload(self, pk, allowed_company_ids) -> dict:
        """SAP's own Return sheet for this entry's document, as data.

        Read from SAP every time rather than snapshotted at posting: the document
        can still be amended in SAP afterwards, and a sheet printed from a stale
        copy is the kind of error nobody notices.
        """
        entry = self._get_scoped(pk, allowed_company_ids)
        if not entry.sap_return_doc_entry:
            raise ValueError(
                f"{entry.entry_no} has no SAP document behind it, so there is no "
                f"Return Note to print."
            )
        payload = self._client(entry.company).goods_return_print(entry.sap_return_doc_entry)
        if not payload:
            raise ValueError(
                f"SAP has no return {entry.sap_return_doc_num or entry.sap_return_doc_entry} "
                f"for {entry.company.code}."
            )
        payload["short_dispatch_id"] = entry.id
        payload["entry_no"] = entry.entry_no
        payload["invoice_doc_num"] = entry.sap_invoice_doc_num
        return payload
