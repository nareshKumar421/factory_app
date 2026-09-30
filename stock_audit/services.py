"""The rules of a stock audit. Views only translate HTTP to and from these.

* An audit is one warehouse of one company, and a warehouse has at most one
  open audit at a time.
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
    AuditStatus, StockAudit, StockAuditCount, StockAuditLine, category_for,
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
        uom=row['uom'][:20],
        sap_qty=row['on_hand'],
        in_sap=in_sap,
    )


def start(company, warehouse_code, warehouse_name, user, notes='') -> StockAudit:
    """Open an audit of ``warehouse_code`` with SAP's figures as they stand."""
    warehouse_code = warehouse_code.strip()
    if StockAudit.objects.filter(company=company, warehouse_code=warehouse_code,
                                 status=AuditStatus.OPEN).exists():
        raise AuditError(f"{warehouse_code} already has an open audit. Close it first.")
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
        raise AuditError(f"{warehouse_code} already has an open audit.") from e
    return audit


def _require_open(audit):
    if audit.status != AuditStatus.OPEN:
        raise AuditError('This audit is closed.')


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


def add_count(line, qty, user, note='') -> StockAuditCount:
    """Add ``qty`` to what has been found of ``line``'s item.

    A count of 0 is a count: "looked, found none" marks the line counted, which
    an uncounted line is not.
    """
    _require_open(line.audit)
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


def void_count(count, user, may_void_others=False) -> StockAuditCount:
    """Take back a count entered by mistake. Kept, marked void, not deleted."""
    _require_open(count.line.audit)
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


def add_item(audit, item_code) -> StockAuditLine:
    """Put an item on the audit that SAP's copy did not list (found on the floor)."""
    _require_open(audit)
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


def close(audit, user) -> StockAudit:
    _require_open(audit)
    audit.status = AuditStatus.CLOSED
    audit.closed_by = user
    audit.closed_at = timezone.now()
    audit.save(update_fields=['status', 'closed_by', 'closed_at'])
    return audit


def summary(audit, with_sap=True) -> dict:
    """Progress by category: lines, lines counted, and lines that differ."""
    counted = Q(counted_qty__isnull=False)
    rows = (audit.lines.values('category')
            .annotate(lines=Count('id'), counted=Count('id', filter=counted))
            .order_by('category'))
    result = {r['category']: {'lines': r['lines'], 'counted': r['counted']} for r in rows}
    if with_sap:
        differ = {
            r['category']: r['n'] for r in
            audit.lines.filter(counted).exclude(counted_qty=F('sap_qty'))
            .values('category').annotate(n=Count('id'))
        }
        for category, part in result.items():
            part['different'] = differ.get(category, 0)
    total = {'lines': sum(p['lines'] for p in result.values()),
             'counted': sum(p['counted'] for p in result.values())}
    if with_sap:
        total['different'] = sum(p['different'] for p in result.values())
    return {'by_category': result, 'total': total}

