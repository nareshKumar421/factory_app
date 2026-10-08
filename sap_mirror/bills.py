"""The copy of the last 30 days of A/R bills, for dispatch when HANA is down.

Dispatch cannot live on a nightly copy: bills are booked the same day, often
after the truck is inside. So the copy is refreshed at every run of
``sync_sap_copy`` (every 15 minutes), and only what changed is read again:

1. SAP is asked for every live bill of the window with its ``UpdateDate`` and
   ``UpdateTS`` -- one light query;
2. a bill that is new, or whose stamp moved, is read in full, the way the
   dispatch reader reads it (header, lines, picking lines);
3. a bill SAP no longer lists -- cancelled, or older than the window -- leaves.

Besides the window, the copy holds every bill still in dispatch planning,
whatever its age (:func:`planned_doc_entries`): planning reaches far back -- on
2026-10-06, 380 of 407 pending plans were older than 30 days -- and the Plan
page asks for exactly those bills.

An A/R invoice's lines cannot change once posted, only its user fields (the
dispatch stamp) can, and those move the stamp; so a bill is read once and then
only when SAP says it changed.

Serving: :func:`list_bills`, :func:`list_bill_lines` and
:func:`list_pickable_lines` answer the reader's reads of the same names, and
return ``None`` -- "ask SAP", i.e. fail as SAP being unavailable -- for a bill
the copy does not hold, rather than claim it does not exist. A single bill
read from the copy is recorded as a :class:`ServedBill`, for ``recheck`` to
compare with SAP once it answers again.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .codec import pack, unpack
from .models import MirrorDataset, MirroredBill, ServedBill, ServedBillOutcome

logger = logging.getLogger(__name__)

BILLS = "bills"
#: How far back the copy goes, by the bill's CreateDate (the user's call).
WINDOW_DAYS = 30
#: Bills read in full per round trip.
CHUNK = 200
#: The reader's own ceiling on a list.
MAX_BILL_ROWS = 20000
#: Plans dispatched this recently stay in the copy: the Plan page opens on the
#: last month of dispatch dates, and a bill invoiced earlier still shows there.
PLANNING_LOOKBACK_DAYS = 45


def planned_doc_entries(company, today):
    """Every bill dispatch planning may ask for, whatever its age.

    Selected for planning, pending or booked (not yet gone), or dispatched in
    the last :data:`PLANNING_LOOKBACK_DAYS` -- what the Plan page and the gate's
    expected-dispatch view list.
    """
    from django.db.models import Q

    from dispatch_plans.models import DispatchPlan, SelectedDispatchBill

    entries = set(
        SelectedDispatchBill.objects.filter(company=company, is_active=True)
        .values_list("sap_invoice_doc_entry", flat=True)
    )
    entries |= set(
        DispatchPlan.objects.filter(company=company, is_active=True)
        .filter(
            Q(booking_status__in=["PENDING", "BOOKED"])
            | Q(dispatch_date__gte=today - timedelta(days=PLANNING_LOOKBACK_DAYS))
        )
        .values_list("sap_invoice_doc_entry", flat=True)
    )
    return {int(entry) for entry in entries if entry}


def window_start(today):
    return today - timedelta(days=WINDOW_DAYS)


# ---------------------------------------------------------------------------
# taking the copy
# ---------------------------------------------------------------------------

def _reader(company_code):
    from dispatch_plans.hana_reader import HanaDispatchBillReader
    from sap_client.context import CompanyContext

    # use_copy=False: a HANA failure half-way must fail this run, not be
    # answered from the copy and written back into it as if SAP had said so.
    return HanaDispatchBillReader(CompanyContext(company_code), use_copy=False)


def refresh(company, state, now) -> int:
    """Bring one company's bill copy up to date; returns how many bills it holds."""
    from .services import KeepCopy

    reader = _reader(company.code)
    today = timezone.localdate(now)
    created_from = window_start(today)

    versions = reader.bill_versions(created_from)
    if not versions and state.row_count:
        raise KeepCopy("SAP answered with no bills; the previous copy was kept.")
    credited = reader.credited_doc_entries(created_from)
    # Older bills still in planning, kept beside the window.
    older = sorted(planned_doc_entries(company, today) - set(versions))
    if older:
        versions.update(reader.bill_versions_for(older))
        credited |= reader.credited_among(older)

    stored = dict(
        MirroredBill.objects.filter(company=company).values_list("doc_entry", "version")
    )
    changed = sorted(
        entry
        for entry, version in versions.items()
        if stored.get(entry) != version or _stamp_is_only_a_date(version, today)
    )

    fetched = []
    for start in range(0, len(changed), CHUNK):
        chunk = changed[start:start + CHUNK]
        bills = reader.list_bills({"doc_entries": chunk, "limit": len(chunk)})
        lines = reader.list_bill_lines_for(chunk)
        pickable = {}
        for line in reader.list_pickable_lines(chunk):
            pickable.setdefault(line["doc_entry"], []).append(line)
        for bill in bills:
            entry = bill["doc_entry"]
            fetched.append(
                _row(company, bill, lines.get(entry, []), pickable.get(entry, []),
                     versions[entry], now)
            )

    gone = set(stored) - set(versions)
    with transaction.atomic():
        if gone:
            MirroredBill.objects.filter(company=company, doc_entry__in=gone).delete()
        if fetched:
            MirroredBill.objects.filter(
                company=company, doc_entry__in=[row.doc_entry for row in fetched]
            ).delete()
            MirroredBill.objects.bulk_create(fetched, batch_size=500)
        bills = MirroredBill.objects.filter(company=company)
        bills.filter(doc_entry__in=credited).exclude(credited=True).update(credited=True)
        bills.exclude(doc_entry__in=credited).exclude(credited=False).update(credited=False)
    logger.info(
        "SAP bill copy for %s: %s bills, %s read again, %s dropped",
        company.code, len(versions), len(fetched), len(gone),
    )
    # SAP has just answered, so anything handed out while it could not can now
    # be checked against it. A failed check stays pending for the next run.
    from .recheck import recheck_served

    try:
        recheck_served(company, reader, now)
    except Exception:  # noqa: BLE001
        logger.exception("Re-check of bills served from the SAP copy failed for %s", company.code)
    return len(versions)


def _stamp_is_only_a_date(version, today):
    # No UpdateTS column: a bill changed today cannot be told apart from one
    # changed again an hour later, so today's are always read again.
    return version.endswith("|") and version.startswith(today.isoformat())


def _row(company, bill, lines, pickable, version, now):
    warehouses = sorted(
        {(line.get("warehouse_code") or "").upper() for line in lines} - {""}
    )
    return MirroredBill(
        company=company,
        doc_entry=bill["doc_entry"],
        doc_num=str(bill.get("doc_num") or "")[:30],
        doc_num_sort=_as_int(bill.get("doc_num")),
        create_date=bill.get("create_date") or None,
        create_time=(bill.get("create_time") or "")[:10],
        branch_id=bill.get("branch_id"),
        branch_name=(bill.get("branch_name") or "")[:200],
        warehouse_codes=(f"|{'|'.join(warehouses)}|" if warehouses else "")[:1000],
        sap_dispatched=bool(bill.get("sap_dispatch_date")),
        version=version[:40],
        bill=pack(bill),
        lines=pack(lines),
        pickable_lines=pack(pickable),
        copied_at=now,
    )


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# serving it
# ---------------------------------------------------------------------------

def _copy(company_code):
    return (
        MirrorDataset.objects.filter(
            company__code=company_code, name=BILLS, synced_at__isnull=False
        )
        .only("id", "company_id", "synced_at")
        .first()
    )


def _as_of(state):
    return timezone.localtime(state.synced_at).isoformat()


def _record_served(state, rows):
    """Note that these bills were handed out from the copy, for the re-check.

    Never allowed to fail the read: the bill was served either way, and an
    outage is the worst moment to turn a working lookup into an error.
    """
    try:
        now = timezone.now()
        for row in rows:
            pending = ServedBill.objects.filter(
                company_id=row.company_id, doc_entry=row.doc_entry,
                outcome=ServedBillOutcome.PENDING,
            )
            if pending.update(last_served_at=now):
                continue
            ServedBill.objects.create(
                company_id=row.company_id, doc_entry=row.doc_entry, doc_num=row.doc_num,
                served_at=now, last_served_at=now, copy_as_of=state.synced_at,
                served={"bill": row.bill, "lines": row.lines},
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not record bills served from the SAP copy")


def list_bills(company_code, filters):
    """The reader's ``list_bills``, from the copy; ``None`` when it cannot say."""
    state = _copy(company_code)
    if state is None:
        return None
    bills = MirroredBill.objects.filter(company_id=state.company_id)

    doc_entries = [int(value) for value in filters.get("doc_entries") or []]
    invoice_doc_num = (filters.get("invoice_doc_num") or "").strip()
    if doc_entries:
        bills = bills.filter(doc_entry__in=doc_entries)
        missing = bills.values("doc_entry").distinct().count() < len(set(doc_entries))
        if missing and not filters.get("partial_ok"):
            # A bill the copy does not hold -- newer than it, or old and out of
            # planning -- is SAP being down, not "no such bill". A caller that
            # only shows the list may take what the copy has instead.
            return None
    elif invoice_doc_num:
        bills = bills.filter(doc_num=invoice_doc_num)
        if not bills.exists():
            return None
    else:
        date_from, date_to = filters["date_from"], filters["date_to"]
        if str(date_to) < window_start(timezone.localdate(state.synced_at)).isoformat():
            return None  # wholly before what the copy holds
        bills = bills.filter(create_date__gte=date_from, create_date__lte=date_to)

    branch = (filters.get("branch") or "").strip()
    if branch:
        from django.db.models import Q

        branch_filter = Q(branch_name__iexact=branch)
        if branch.lstrip("-").isdigit():
            branch_filter |= Q(branch_id=int(branch))
        bills = bills.filter(branch_filter)
    warehouse = (filters.get("warehouse") or "").strip().upper()
    if warehouse:
        bills = bills.filter(warehouse_codes__contains=f"|{warehouse}|")
    if filters.get("exclude_sap_dispatched"):
        bills = bills.filter(sap_dispatched=False)
    if filters.get("exclude_credited"):
        bills = bills.filter(credited=False)

    raw_limit = filters.get("limit")
    limit = min(max(int(raw_limit) if raw_limit else MAX_BILL_ROWS, 1), MAX_BILL_ROWS)
    as_of = _as_of(state)
    bills = bills.order_by("-create_date", "-create_time", "-doc_num_sort")
    if (doc_entries or invoice_doc_num) and not filters.get("partial_ok"):
        # Particular bills, which is how a docking, a barcode session, a bill
        # summary or a short dispatch takes one: worth checking afterwards. A
        # list only shown (partial_ok) is not -- nothing was done with it.
        rows = list(bills[:limit])
        _record_served(state, rows)
        return [{**unpack(row.bill), "sap_copy_as_of": as_of} for row in rows]
    return [
        {**unpack(bill), "sap_copy_as_of": as_of}
        for bill in bills.values_list("bill", flat=True)[:limit]
    ]


def list_bill_lines(company_code, doc_entry):
    state = _copy(company_code)
    if state is None:
        return None
    row = MirroredBill.objects.filter(company_id=state.company_id, doc_entry=doc_entry).first()
    if row is None:
        return None
    _record_served(state, [row])
    return unpack(row.lines)


def list_pickable_lines(company_code, doc_entries):
    state = _copy(company_code)
    if state is None:
        return None
    entries = [int(entry) for entry in doc_entries]
    rows = list(
        MirroredBill.objects.filter(company_id=state.company_id, doc_entry__in=entries)
        .order_by("doc_num_sort")
    )
    if len(rows) < len(set(entries)):
        return None
    _record_served(state, rows)
    # SAP's own order: by DocNum, then line.
    return [line for row in rows for line in unpack(row.pickable_lines)]
