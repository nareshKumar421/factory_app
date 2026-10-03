"""The copy of open purchase orders, for the gate when HANA is down.

A truck at the gate is entered against its supplier's open POs. Those change
through the day -- new POs are raised, lines are received against -- so the
copy is refreshed at every run of ``sync_sap_copy`` (every 15 minutes), and
only what changed is read again:

1. SAP is asked, in one light query, for every PO with an open line and its
   version: ``UpdateDate|UpdateTS`` (edited) plus the open quantity and the
   number of open lines (received against);
2. a PO that is new, or whose version moved, has its open lines read again;
3. a PO with nothing left open leaves.

Quantities can be up to 15 minutes old. That is safe: the GRPO that follows
is checked by SAP itself when it is posted, and refused if the quantity is no
longer open.

Serving: :func:`open_po_rows` answers the reader's three gate reads with rows
in its own shape; ``None`` -- "ask SAP", i.e. fail as SAP being unavailable --
when there is no copy, or for a PO number the copy does not hold.
"""

import logging

from django.db import transaction

from .codec import pack, unpack
from .models import MirrorDataset, MirroredPurchaseOrder

logger = logging.getLogger(__name__)

PURCHASE_ORDERS = "purchase_orders"
#: POs read in full per round trip.
CHUNK = 200
#: Index of the row's DocEntry, DocNum, supplier and the finished-goods flag.
DOC_NUM, SUPPLIER, DOC_ENTRY, IS_FG = 0, 1, 10, 19


def _reader(company_code):
    from sap_client.context import CompanyContext
    from sap_client.hana.po_reader import HanaPOReader

    # use_copy=False: a HANA failure half-way fails this run, not the copy.
    return HanaPOReader(CompanyContext(company_code), use_copy=False)


def refresh(company, state, now) -> int:
    """Bring one company's open-PO copy up to date; returns how many POs it holds."""
    from .services import KeepCopy

    reader = _reader(company.code)
    versions = reader.open_po_versions()
    if not versions and state.row_count:
        raise KeepCopy("SAP answered with no open purchase orders; the previous copy was kept.")

    stored = dict(
        MirroredPurchaseOrder.objects.filter(company=company).values_list("doc_entry", "version")
    )
    changed = sorted(entry for entry, version in versions.items() if stored.get(entry) != version)

    fetched = []
    for start in range(0, len(changed), CHUNK):
        lines_by_po = {}
        for row in reader.open_po_rows_for(changed[start:start + CHUNK]):
            lines_by_po.setdefault(int(row[DOC_ENTRY]), []).append(row)
        for entry, rows in lines_by_po.items():
            fetched.append(MirroredPurchaseOrder(
                company=company,
                doc_entry=entry,
                doc_num=str(rows[0][DOC_NUM])[:30],
                supplier_code=str(rows[0][SUPPLIER] or "")[:50],
                version=versions[entry][:80],
                rows=pack(rows),
                copied_at=now,
            ))

    gone = set(stored) - set(versions)
    with transaction.atomic():
        if gone:
            MirroredPurchaseOrder.objects.filter(company=company, doc_entry__in=gone).delete()
        if fetched:
            MirroredPurchaseOrder.objects.filter(
                company=company, doc_entry__in=[po.doc_entry for po in fetched]
            ).delete()
            MirroredPurchaseOrder.objects.bulk_create(fetched, batch_size=500)
    logger.info(
        "SAP PO copy for %s: %s open POs, %s read again, %s closed",
        company.code, len(versions), len(fetched), len(gone),
    )
    return len(versions)


def open_po_rows(company_code, *, supplier_code=None, po_number=None, fg_only=False):
    """Open PO rows from the copy, in the reader's shape; ``None`` when it cannot say."""
    state = (
        MirrorDataset.objects.filter(
            company__code=company_code, name=PURCHASE_ORDERS, synced_at__isnull=False
        )
        .only("id", "company_id")
        .first()
    )
    if state is None:
        return None
    pos = MirroredPurchaseOrder.objects.filter(company_id=state.company_id)
    if po_number is not None:
        pos = pos.filter(doc_num=str(po_number).strip())
        if not pos.exists():
            return None  # newer than the copy, or closed: SAP has to say which
    else:
        # A supplier with nothing open is an answer, not a gap.
        pos = pos.filter(supplier_code=supplier_code)
    rows = [row for po in pos.order_by("doc_entry") for row in unpack(po.rows)]
    if fg_only:
        rows = [row for row in rows if row[IS_FG]]
    return [row[:IS_FG] for row in rows]
