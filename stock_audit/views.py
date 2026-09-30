"""HTTP for stock audits. The rules are in ``services``; this only translates.

Every audit is the signed-in company's (the ``Company-Code`` header), so an
Oil login audits Oil's SAP warehouses and a Beverages login Beverages'.
"""
import csv
from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .models import (
    ACTIVE_STATUSES, AuditStatus, ItemCategory, SapPosting, StockAudit, StockAuditCount,
    StockAuditLine,
)
from .permissions import (
    APPROVE, COUNT, MANAGE, POST_TO_SAP, CanApproveStockAudit, CanCountStockAudit,
    CanManageStockAudit, CanPostStockAuditToSap, CanViewStockAudit, has, sees_sap,
)

PAGE_SIZE = 50


def _sap_unavailable(e):
    return Response({'detail': str(e) or 'SAP is not answering. Try again in a minute.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _refused(e):
    return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


def _qty(value):
    return None if value is None else f"{value.normalize():f}"


def _person(user):
    return (getattr(user, 'full_name', '') or getattr(user, 'email', '')) if user else ''


def _as_approver(request, audit):
    """Counting on an audit awaiting approval is an approver's correction."""
    return audit.status == AuditStatus.SUBMITTED and has(request.user, APPROVE)


def _actions(audit, user):
    """What this user may do to the audit now, so the page shows only those buttons."""
    status_ = audit.status
    counts_exist = StockAuditCount.objects.filter(line__audit=audit).exists()
    return {
        'count': (status_ == AuditStatus.OPEN and has(user, COUNT, MANAGE, APPROVE))
        or (status_ == AuditStatus.SUBMITTED and has(user, APPROVE)),
        'refresh': status_ == AuditStatus.OPEN and not counts_exist and has(user, MANAGE),
        'complete': status_ == AuditStatus.OPEN and has(user, COUNT, MANAGE),
        'approve': status_ == AuditStatus.SUBMITTED and has(user, APPROVE),
        'post_to_sap': (status_ == AuditStatus.APPROVED and audit.sap_posting != SapPosting.DONE
                        and has(user, POST_TO_SAP)),
        'void_any': has(user, MANAGE, APPROVE),
    }


def _audit_json(audit, request, with_summary=False):
    data = {
        'id': audit.id,
        'warehouse_code': audit.warehouse_code,
        'warehouse_name': audit.warehouse_name,
        'status': audit.status,
        'notes': audit.notes,
        'snapshot_at': audit.snapshot_at,
        'started_by': _person(audit.started_by),
        'started_at': audit.started_at,
        'closed_by': _person(audit.closed_by),
        'closed_at': audit.closed_at,
        'completed_by': _person(audit.completed_by),
        'completed_at': audit.completed_at,
        'approved_by': _person(audit.approved_by),
        'approved_at': audit.approved_at,
        'approval_comment': audit.approval_comment,
        'rejected_by': _person(audit.rejected_by),
        'rejected_at': audit.rejected_at,
        'rejection_reason': audit.rejection_reason,
        'sap_posting': audit.sap_posting,
        'sap_doc_num': audit.sap_doc_num,
        'sap_posted_at': audit.sap_posted_at,
        'sap_posting_error': audit.sap_posting_error,
        'sap_posted_by': _person(audit.sap_posted_by),
        # What was sent: shown once posted, and after an attempt SAP refused
        # or did not answer, so it can be checked against SAP.
        'sap_posted_lines': audit.sap_posted_lines or [],
    }
    if with_summary:
        data['summary'] = services.summary(audit, with_sap=sees_sap(request.user))
        data['actions'] = _actions(audit, request.user)
    return data


def _line_json(line, see_sap):
    data = {
        'id': line.id,
        'item_code': line.item_code,
        'item_name': line.item_name,
        'category': line.category,
        'item_group_name': line.item_group_name or line.category,
        'is_batch': line.is_batch,
        'uom': line.uom,
        'in_sap': line.in_sap,
        'counted_qty': _qty(line.counted_qty),
        'count_entries': getattr(line, 'entries', None),
    }
    if see_sap:
        data['sap_qty'] = _qty(line.sap_qty)
        data['difference'] = _qty(line.difference)
    return data


def _count_json(count, request):
    return {
        'id': count.id,
        'qty': _qty(count.qty),
        'note': count.note,
        'counted_by': _person(count.counted_by),
        'counted_at': count.counted_at,
        'voided': count.voided_at is not None,
        'voided_by': _person(count.voided_by),
        'mine': count.counted_by_id == request.user.id,
    }


def _company_audit(request, audit_id):
    return get_object_or_404(StockAudit, pk=audit_id, company=request.company.company)


def _company_line(request, audit_id, line_id):
    return get_object_or_404(
        StockAuditLine.objects.select_related('audit'),
        pk=line_id, audit_id=audit_id, audit__company=request.company.company)


class _Base(APIView):
    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanViewStockAudit()]


class WarehousesAPI(_Base):
    """The company's active SAP warehouses, and which of them are being audited."""

    def get(self, request):
        company = request.company.company
        try:
            warehouses = SAPClient(company.code).get_active_warehouses()
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        open_audits = dict(StockAudit.objects.filter(
            company=company, status__in=ACTIVE_STATUSES).values_list('warehouse_code', 'id'))
        return Response([
            {'code': w.warehouse_code, 'name': w.warehouse_name,
             'open_audit_id': open_audits.get(w.warehouse_code)}
            for w in warehouses
        ])


class AuditListCreateAPI(_Base):
    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated(), HasCompanyContext(), CanManageStockAudit()]
        return super().get_permissions()

    def get(self, request):
        audits = (StockAudit.objects.filter(company=request.company.company)
                  .select_related('started_by', 'closed_by')
                  .annotate(line_count=Count('lines'),
                            counted_count=Count('lines', filter=Q(lines__counted_qty__isnull=False))))
        if request.GET.get('status') in AuditStatus.values:
            audits = audits.filter(status=request.GET['status'])
        return Response([
            {**_audit_json(a, request), 'lines': a.line_count, 'counted': a.counted_count}
            for a in audits[:100]
        ])

    def post(self, request):
        code = (request.data.get('warehouse_code') or '').strip()
        if not code:
            return _refused('Pick the warehouse to audit.')
        try:
            audit = services.start(
                request.company.company, code, request.data.get('warehouse_name') or '',
                request.user, request.data.get('notes') or '')
        except services.AuditError as e:
            return _refused(e)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        return Response(_audit_json(audit, request, with_summary=True),
                        status=status.HTTP_201_CREATED)


class AuditDetailAPI(_Base):
    def get(self, request, audit_id):
        return Response(_audit_json(_company_audit(request, audit_id), request, with_summary=True))


class AuditLinesAPI(_Base):
    """``?search=&group=<SAP item group>&category=RM|PM|FG|OTHER&state=uncounted|counted|different&page=``"""

    def get(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        see_sap = sees_sap(request.user)
        lines = audit.lines.annotate(
            entries=Count('counts', filter=Q(counts__voided_at__isnull=True)))

        search = (request.GET.get('search') or '').strip()
        if search:
            lines = lines.filter(Q(item_code__icontains=search) | Q(item_name__icontains=search))
        category = request.GET.get('category')
        if category in ItemCategory.values:
            lines = lines.filter(category=category)
        group = (request.GET.get('group') or '').strip()
        if group:
            lines = lines.filter(Q(item_group_name=group) | Q(item_group_name='', category=group))
        state = request.GET.get('state')
        if state == 'uncounted':
            lines = lines.filter(counted_qty__isnull=True)
        elif state == 'counted':
            lines = lines.filter(counted_qty__isnull=False)
        elif state == 'different':
            if not see_sap:
                return _refused('Only someone who can see SAP quantities can list differences.')
            lines = lines.filter(counted_qty__isnull=False).exclude(counted_qty=F('sap_qty'))

        total = lines.count()
        try:
            page = max(1, int(request.GET.get('page') or 1))
        except ValueError:
            page = 1
        start = (page - 1) * PAGE_SIZE
        return Response({
            'count': total,
            'page': page,
            'page_size': PAGE_SIZE,
            'sees_sap': see_sap,
            'results': [_line_json(line, see_sap) for line in lines[start:start + PAGE_SIZE]],
        })


class LineCountsAPI(_Base):
    """A line's counts, oldest first; POST ``{qty, note}`` adds one."""

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated(), HasCompanyContext(), CanCountStockAudit()]
        return super().get_permissions()

    def get(self, request, audit_id, line_id):
        line = _company_line(request, audit_id, line_id)
        counts = line.counts.select_related('counted_by', 'voided_by')
        return Response([_count_json(c, request) for c in counts])

    def post(self, request, audit_id, line_id):
        line = _company_line(request, audit_id, line_id)
        raw = str(request.data.get('qty', '')).strip()
        if not raw:
            return _refused('Enter the quantity found, in figures.')
        try:
            qty = Decimal(raw)
        except (InvalidOperation, ValueError):
            return _refused('Enter the quantity found, in figures.')
        if not qty.is_finite() or abs(qty) >= Decimal('1e15'):
            return _refused('Enter the quantity found, in figures.')
        try:
            services.add_count(line, qty, request.user, request.data.get('note') or '',
                               as_approver=_as_approver(request, line.audit))
        except services.AuditError as e:
            return _refused(e)
        line = StockAuditLine.objects.annotate(
            entries=Count('counts', filter=Q(counts__voided_at__isnull=True))).get(pk=line.pk)
        return Response(_line_json(line, sees_sap(request.user)), status=status.HTTP_201_CREATED)


class CountVoidAPI(_Base):
    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanCountStockAudit()]

    def post(self, request, audit_id, count_id):
        count = get_object_or_404(
            StockAuditCount.objects.select_related('line__audit'),
            pk=count_id, line__audit_id=audit_id, line__audit__company=request.company.company)
        try:
            services.void_count(count, request.user,
                                may_void_others=has(request.user, MANAGE, APPROVE),
                                as_approver=_as_approver(request, count.line.audit))
        except services.AuditError as e:
            return _refused(e)
        line = StockAuditLine.objects.annotate(
            entries=Count('counts', filter=Q(counts__voided_at__isnull=True))).get(pk=count.line_id)
        return Response(_line_json(line, sees_sap(request.user)))


class AuditItemsAPI(_Base):
    """GET ``?search=`` SAP's stock items; POST ``{item_code}`` puts one on the audit."""

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanCountStockAudit()]

    def get(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        search = (request.GET.get('search') or '').strip()
        if len(search) < 2:
            return Response([])
        try:
            rows = services._reader(audit.company).search_items(audit.warehouse_code, search)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        on_audit = set(audit.lines.filter(item_code__in=[r['item_code'] for r in rows])
                       .values_list('item_code', flat=True))
        return Response([
            {'item_code': r['item_code'], 'item_name': r['item_name'], 'uom': r['uom'],
             'on_audit': r['item_code'] in on_audit}
            for r in rows
        ])

    def post(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        code = (request.data.get('item_code') or '').strip()
        if not code:
            return _refused('Pick the item to add.')
        try:
            line = services.add_item(audit, code, as_approver=_as_approver(request, audit))
        except services.AuditError as e:
            return _refused(e)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        line.entries = line.counts.filter(voided_at__isnull=True).count()
        return Response(_line_json(line, sees_sap(request.user)), status=status.HTTP_201_CREATED)


class AuditRefreshAPI(_Base):
    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanManageStockAudit()]

    def post(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        try:
            services.refresh(audit, request.user)
        except services.AuditError as e:
            return _refused(e)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        return Response(_audit_json(audit, request, with_summary=True))


class _AuditAction(_Base):
    """POST one step of the audit's workflow; answers with the audit as it now is."""
    permission = CanViewStockAudit

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), self.permission()]

    def act(self, request, audit):
        raise NotImplementedError

    def post(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        try:
            self.act(request, audit)
        except services.AuditError as e:
            return _refused(e)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)
        audit.refresh_from_db()
        return Response(_audit_json(audit, request, with_summary=True))


class AuditCompleteAPI(_AuditAction):
    permission = CanCountStockAudit

    def act(self, request, audit):
        if not has(request.user, COUNT, MANAGE):
            raise services.AuditError('Only an auditor can complete the audit.')
        services.complete(audit, request.user)


class AuditApproveAPI(_AuditAction):
    permission = CanApproveStockAudit

    def act(self, request, audit):
        services.approve(audit, request.user, request.data.get('comment') or '')


class AuditRejectAPI(_AuditAction):
    permission = CanApproveStockAudit

    def act(self, request, audit):
        services.reject(audit, request.user,
                        request.data.get('comment') or request.data.get('reason') or '')


class AuditPostToSapAPI(_AuditAction):
    """GET: what the Inventory Posting would change. POST ``{confirm_unknown}``: post it."""
    permission = CanPostStockAuditToSap

    def get(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        if audit.status != AuditStatus.APPROVED:
            return _refused('Only an approved audit can be posted to SAP.')
        try:
            preview = services.posting_preview(audit)
        except (SAPConnectionError, SAPDataError) as e:
            return _sap_unavailable(e)

        def line_json(line):
            return {**{k: v for k, v in line.items() if k not in ('sap_qty', 'counted_qty', 'batches')},
                    'sap_qty': _qty(line['sap_qty']), 'counted_qty': _qty(line['counted_qty']),
                    'difference': _qty(line['counted_qty'] - line['sap_qty']),
                    'batches': [{'batch': b['batch'], 'sap_qty': _qty(b['sap_qty']),
                                 'counted_qty': _qty(b['counted_qty'])}
                                for b in line.get('batches', [])]}
        return Response({
            'lines': [line_json(l) for l in preview['lines']],
            'blocked': [{**line_json(l), 'reason': l['reason']} for l in preview['blocked']],
        })

    def act(self, request, audit):
        services.post_to_sap(audit, request.user,
                             confirm_unknown=bool(request.data.get('confirm_unknown')))


class AuditExportAPI(_Base):
    """The whole audit as CSV — SAP and difference columns only for those who may see them."""

    def get(self, request, audit_id):
        audit = _company_audit(request, audit_id)
        see_sap = sees_sap(request.user)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            f'attachment; filename="stock-audit-{audit.warehouse_code}-{audit.id}.csv"')
        writer = csv.writer(response)
        head = ['Item code', 'Item name', 'Item group', 'UoM', 'On hand (physical)']
        writer.writerow(head + (['SAP', 'Difference'] if see_sap else []) + ['Not in SAP copy'])
        for line in audit.lines.all():
            row = [line.item_code, line.item_name, line.item_group_name or line.category, line.uom,
                   _qty(line.counted_qty) or '']
            if see_sap:
                row += [_qty(line.sap_qty), _qty(line.difference) or '']
            writer.writerow(row + ['' if line.in_sap else 'Yes'])
        return response
