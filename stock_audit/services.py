"""The rules of a stock audit. Views only translate HTTP to and from these.

* An audit is one warehouse of one company, and a warehouse has at most one
  audit in progress (open or awaiting approval) at a time.
* Open -> Complete -> Approve. A rejection sends it back open, with the
  reason. Auditors count while it is open; an approver may still correct the
  counts while it waits for them.
* Once approved, its RM and PM differences can be posted to SAP as one
  Inventory Posting (:func:`post_to_sap`) — the only write to SAP here.
* SAP's figures are copied when the audit starts. Until the first count is
  entered the copy can be taken again (somebody posted a last GRPO before the
  lock); after that it is fixed, because the counts are against it.
* A count adds to the line. The line's figure is the sum of its live counts,
  recomputed from them on every change, so it can never drift from the entries
  a person can see.
* A closed audit takes no more counts.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from .hana_reader import StockAuditReader
from .models import (
    ACTIVE_STATUSES, AuditStatus, ItemCategory, SapPosting, StockAudit, StockAuditCount,
    StockAuditLine, category_for,
)

ZERO = Decimal('0')


class AuditError(ValueError):
    """A request the audit's rules refuse; the message is shown as it is."""


def _reader(company):
    return StockAuditReader(company.code)


def _line_from_sap(audit, row, in_sap=True):
    return StockAuditLine(
        audit=audit,
        item_code=row['item_code'],
        item_name=row['item_name'][:255],
        item_group=row['item_group'],
        category=category_for(row['item_group']),
        item_group_name=(row.get('item_group_name') or '')[:100],
        is_batch=bool(row.get('is_batch')),
        uom=row['uom'][:20],
        sap_qty=row['on_hand'],
        in_sap=in_sap,
    )


def start(company, warehouse_code, warehouse_name, user, notes='') -> StockAudit:
    """Open an audit of ``warehouse_code`` with SAP's figures as they stand."""
    warehouse_code = warehouse_code.strip()
    if StockAudit.objects.filter(company=company, warehouse_code=warehouse_code,
                                 status__in=ACTIVE_STATUSES).exists():
        raise AuditError(f"{warehouse_code} already has an audit in progress.")
    rows = _reader(company).warehouse_stock(warehouse_code)   # before any write
    try:
        with transaction.atomic():
            audit = StockAudit.objects.create(
                company=company, warehouse_code=warehouse_code,
                warehouse_name=warehouse_name[:150], notes=notes[:300],
                snapshot_at=timezone.now(), started_by=user)
            StockAuditLine.objects.bulk_create(
                [_line_from_sap(audit, row) for row in rows], batch_size=1000)
    except IntegrityError as e:
        # Two people pressed Start at once; the database let one through.
        raise AuditError(f"{warehouse_code} already has an audit in progress.") from e
    return audit


def _require_open(audit):
    if audit.status != AuditStatus.OPEN:
        raise AuditError('This audit is not open.')


def _require_countable(audit, as_approver=False):
    """Auditors count an open audit; an approver may correct one awaiting them."""
    if audit.status == AuditStatus.OPEN:
        return
    if audit.status == AuditStatus.SUBMITTED and as_approver:
        return
    if audit.status == AuditStatus.SUBMITTED:
        raise AuditError('This audit is waiting for approval; only an approver can change it now.')
    raise AuditError('This audit is finished and takes no more counts.')


def refresh(audit, user) -> StockAudit:
    """Take SAP's figures again — only before anything has been counted."""
    _require_open(audit)
    if StockAuditCount.objects.filter(line__audit=audit).exists():
        raise AuditError("Counting has started, so SAP's figures are fixed for this audit.")
    rows = _reader(audit.company).warehouse_stock(audit.warehouse_code)
    with transaction.atomic():
        audit.lines.all().delete()
        StockAuditLine.objects.bulk_create(
            [_line_from_sap(audit, row) for row in rows], batch_size=1000)
        audit.snapshot_at = timezone.now()
        audit.save(update_fields=['snapshot_at'])
    return audit


def _recount(line):
    total = line.counts.filter(voided_at__isnull=True).aggregate(t=Sum('qty'))['t']
    line.counted_qty = total   # None once every count is voided: uncounted again
    line.save(update_fields=['counted_qty'])
    return line


def add_count(line, qty, user, note='', as_approver=False) -> StockAuditCount:
    """Add ``qty`` to what has been found of ``line``'s item.

    A count of 0 is a count: "looked, found none" marks the line counted, which
    an uncounted line is not.
    """
    _require_countable(line.audit, as_approver)
    with transaction.atomic():
        line = StockAuditLine.objects.select_for_update().get(pk=line.pk)
        # A removal takes back part of what was found; it cannot take the
        # floor below nothing.
        if qty < 0 and (line.counted_qty or ZERO) + qty < 0:
            raise AuditError(
                f"Only {(line.counted_qty or ZERO).normalize():f} on hand to remove from.")
        count = StockAuditCount.objects.create(
            line=line, qty=qty, note=note.strip()[:200], counted_by=user)
        _recount(line)
    return count


def void_count(count, user, may_void_others=False, as_approver=False) -> StockAuditCount:
    """Take back a count entered by mistake. Kept, marked void, not deleted."""
    _require_countable(count.line.audit, as_approver)
    if count.voided_at:
        raise AuditError('That count was already taken back.')
    if count.counted_by_id != user.id and not may_void_others:
        raise AuditError('Only the person who entered a count, or an audit manager, can take it back.')
    with transaction.atomic():
        line = StockAuditLine.objects.select_for_update().get(pk=count.line_id)
        if (line.counted_qty or ZERO) - count.qty < 0:
            raise AuditError('Take back the removal after it first: on hand cannot go below nothing.')
        count.voided_by = user
        count.voided_at = timezone.now()
        count.save(update_fields=['voided_by', 'voided_at'])
        _recount(line)
    return count


def add_item(audit, item_code, as_approver=False) -> StockAuditLine:
    """Put an item on the audit that SAP's copy did not list (found on the floor)."""
    _require_countable(audit, as_approver)
    item_code = item_code.strip()
    existing = audit.lines.filter(item_code=item_code).first()
    if existing:
        return existing
    row = _reader(audit.company).item(audit.warehouse_code, item_code)
    if row is None:
        raise AuditError(f"{item_code} is not a stock item in SAP.")
    # What SAP holds of it now; a line added this way was not in the copy.
    line = _line_from_sap(audit, row, in_sap=False)
    line.save()
    return line


def complete(audit, user) -> StockAudit:
    """The count is done: hand it to an approver."""
    _require_open(audit)
    audit.status = AuditStatus.SUBMITTED
    audit.completed_by = user
    audit.completed_at = timezone.now()
    audit.save(update_fields=['status', 'completed_by', 'completed_at'])
    return audit


def approve(audit, user) -> StockAudit:
    if audit.status != AuditStatus.SUBMITTED:
        raise AuditError('Only a completed audit can be approved.')
    audit.status = AuditStatus.APPROVED
    audit.approved_by = user
    audit.approved_at = timezone.now()
    audit.save(update_fields=['status', 'approved_by', 'approved_at'])
    return audit


def reject(audit, user, reason) -> StockAudit:
    """Send it back to the auditors, saying what to look at again."""
    if audit.status != AuditStatus.SUBMITTED:
        raise AuditError('Only a completed audit can be rejected.')
    reason = (reason or '').strip()
    if not reason:
        raise AuditError('Say what needs to be counted again.')
    audit.status = AuditStatus.OPEN
    audit.rejected_by = user
    audit.rejected_at = timezone.now()
    audit.rejection_reason = reason[:300]
    audit.save(update_fields=['status', 'rejected_by', 'rejected_at', 'rejection_reason'])
    return audit


# ---------------------------------------------------------------------------
# Posting the RM and PM differences to SAP
# ---------------------------------------------------------------------------

#: The categories whose differences are posted to SAP.
POSTED_CATEGORIES = (ItemCategory.RM, ItemCategory.PM)


def _spread_over_batches(counted, batches):
    """Each batch's counted quantity, so the item's total is ``counted``.

    The difference goes the way stock is used: a shortage comes off the oldest
    batch first, spilling onto the next when one runs out; an excess goes onto
    the newest. Returns ``[(batch, now, counted)]`` for every batch that changes.
    """
    held = sum((b['qty'] for b in batches), ZERO)
    difference = counted - held
    if difference == 0:
        return []
    if not batches:
        raise AuditError('SAP holds no batch of it here to put the difference on.')
    new = [b['qty'] for b in batches]
    if difference > 0:
        new[-1] += difference
    else:
        short = -difference
        for i, batch in enumerate(batches):
            take = min(batch['qty'], short)
            new[i] -= take
            short -= take
            if short == 0:
                break
    return [(b['batch'], b['qty'], n) for b, n in zip(batches, new) if n != b['qty']]


def posting_preview(audit) -> dict:
    """What an Inventory Posting of this audit would change in SAP.

    Counted RM and PM lines whose count differs from SAP's figure. A line that
    cannot be posted (a batch item with no batch to put an excess on) is listed
    under ``blocked`` with the reason, and left out of ``lines``.
    """
    reader = _reader(audit.company)
    lines, blocked = [], []
    candidates = (audit.lines.filter(category__in=POSTED_CATEGORIES, counted_qty__isnull=False)
                  .exclude(counted_qty=F('sap_qty')).order_by('item_code'))
    for line in candidates:
        entry = {'line_id': line.id, 'item_code': line.item_code, 'item_name': line.item_name,
                 'category': line.category, 'uom': line.uom, 'sap_qty': line.sap_qty,
                 'counted_qty': line.counted_qty, 'batches': []}
        if line.is_batch:
            try:
                changes = _spread_over_batches(
                    line.counted_qty, reader.batches(audit.warehouse_code, line.item_code))
            except AuditError as e:
                blocked.append({**entry, 'reason': str(e)})
                continue
            entry['batches'] = [{'batch': b, 'sap_qty': now, 'counted_qty': new}
                                for b, now, new in changes]
        lines.append(entry)
    return {'lines': lines, 'blocked': blocked}


def _posting_payload(audit, preview, branch) -> dict:
    payload = {
        'CountDate': timezone.localdate().isoformat(),
        'Remarks': f'FactoryFlow stock audit #{audit.id} of {audit.warehouse_code}'[:250],
        'InventoryPostingLines': [],
    }
    if branch is not None:
        payload['BranchID'] = branch
    for n, line in enumerate(preview['lines'], start=1):
        posting_line = {
            'LineNumber': n,
            'ItemCode': line['item_code'],
            'WarehouseCode': audit.warehouse_code,
            'CountedQuantity': float(line['counted_qty']),
        }
        if line['batches']:
            # Every batch that changes, at its counted quantity.
            posting_line['InventoryPostingBatchNumbers'] = [
                {'BatchNumber': b['batch'], 'Quantity': float(b['counted_qty']),
                 'BaseLineNumber': n}
                for b in line['batches']
            ]
        payload['InventoryPostingLines'].append(posting_line)
    return payload


def post_to_sap(audit, user, confirm_unknown=False) -> StockAudit:
    """Post the approved audit's RM and PM differences to SAP, once.

    Claimed before SAP is asked (see :class:`SapPosting`). A posting whose
    request got no answer is only sent again when ``confirm_unknown`` says
    somebody has checked SAP and it is not there.
    """
    from sap_client.exceptions import SAPValidationError
    from sap_client.service_layer.inventory_posting_writer import InventoryPostingWriter

    if audit.status != AuditStatus.APPROVED:
        raise AuditError('Only an approved audit can be posted to SAP.')
    if audit.sap_posting == SapPosting.DONE:
        raise AuditError(f'Already posted to SAP as document {audit.sap_doc_num}.')
    if audit.sap_posting == SapPosting.UNKNOWN and not confirm_unknown:
        raise AuditError('The last posting got no answer from SAP and may have gone through. '
                         'Check SAP for it before posting again.')

    reader = _reader(audit.company)
    preview = posting_preview(audit)
    if preview['blocked']:
        raise AuditError('Some lines cannot be posted: ' + '; '.join(
            f"{b['item_code']}: {b['reason']}" for b in preview['blocked']))
    if not preview['lines']:
        raise AuditError('No RM or PM line differs from SAP, so there is nothing to post.')
    payload = _posting_payload(audit, preview, reader.warehouse_branch(audit.warehouse_code))

    # The claim: only one request moves the audit into POSTING.
    claimed = (StockAudit.objects.filter(pk=audit.pk, status=AuditStatus.APPROVED)
               .exclude(sap_posting__in=[SapPosting.POSTING, SapPosting.DONE])
               .update(sap_posting=SapPosting.POSTING, sap_posting_payload=payload,
                       sap_posting_error='', sap_posted_by=user))
    if not claimed:
        raise AuditError('This audit is being posted to SAP right now.')
    audit.refresh_from_db()

    from sap_client.context import CompanyContext
    writer = InventoryPostingWriter(CompanyContext(audit.company.code))
    try:
        result = writer.create(payload)
    except SAPValidationError as e:
        # SAP answered and refused: nothing was posted, so it can be tried again.
        audit.sap_posting, audit.sap_posting_error = SapPosting.FAILED, str(e)[:500]
        audit.save(update_fields=['sap_posting', 'sap_posting_error'])
        raise AuditError(f'SAP refused the posting: {e}') from e
    except Exception as e:
        audit.sap_posting = SapPosting.UNKNOWN
        audit.sap_posting_error = (str(e) or type(e).__name__)[:500]
        audit.save(update_fields=['sap_posting', 'sap_posting_error'])
        raise AuditError('SAP did not answer. It may have posted: check SAP before posting '
                         'again.') from e
    audit.sap_posting = SapPosting.DONE
    audit.sap_posted_at = timezone.now()
    audit.sap_doc_entry = result.get('DocumentEntry') or result.get('DocEntry')
    audit.sap_doc_num = str(result.get('DocumentNumber') or result.get('DocNum') or '')
    audit.save(update_fields=['sap_posting', 'sap_posted_at', 'sap_doc_entry', 'sap_doc_num'])
    return audit


def summary(audit, with_sap=True) -> dict:
    """Progress by SAP item group: lines, lines counted, and lines that differ."""
    counted = Q(counted_qty__isnull=False)
    rows = (audit.lines.values('item_group_name', 'category')
            .annotate(lines=Count('id'), counted=Count('id', filter=counted)))
    groups = {}
    for r in rows:
        name = r['item_group_name'] or r['category']
        part = groups.setdefault(name, {'lines': 0, 'counted': 0, 'category': r['category']})
        part['lines'] += r['lines']
        part['counted'] += r['counted']
    if with_sap:
        for part in groups.values():
            part['different'] = 0
        for r in (audit.lines.filter(counted).exclude(counted_qty=F('sap_qty'))
                  .values('item_group_name', 'category').annotate(n=Count('id'))):
            groups[r['item_group_name'] or r['category']]['different'] += r['n']
    total = {'lines': sum(p['lines'] for p in groups.values()),
             'counted': sum(p['counted'] for p in groups.values())}
    if with_sap:
        total['different'] = sum(p['different'] for p in groups.values())
    ordered = dict(sorted(groups.items(), key=lambda kv: (-kv[1]['lines'], kv[0])))
    return {'by_group': ordered, 'total': total}
