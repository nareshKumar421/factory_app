"""BOM change requests: raise, approve, reject, cancel, and push to SAP.

Ported from SAP Portal's ``/api/bom-requests`` (``backend_v1/server.js`` lines
305-535) and its ``pushBomToSap`` (lines 485-519). Two portal bugs are gone:

* **Direct create / update left a live request behind when SAP refused.** The
  portal inserted a PENDING row, then pushed, and on failure the row stayed
  PENDING in everyone's queue (lines 316-327, 340-349). Here the row, its lines
  and the push are one transaction: SAP refusing leaves nothing.
* **An admin's create / update pushed to SAP before saving anything** (lines
  366-367, 401-402), so a failed save left a BOM in SAP that nobody had a
  record of. Here every database write and guard comes first and SAP is called
  last, the way every JI SAP write is ordered.

Dedupe, in the order ``sap-integration`` sets:

1. The request row is locked (``select_for_update(of=("self",))``) and its status
   re-checked, so two people pressing the final approve at once cannot both post.
2. A new tree (CREATE) is refused with 409 when SAP already has one for the
   item -- asked of HANA (``OITT`` by code). If that question cannot be answered
   nothing is posted. SAP keys a tree by its item, so there is no reference to
   store: the item code *is* the reference, and SAP itself refuses a second
   tree under it.
3. A replacement (UPDATE) reads the tree SAP holds right before replacing it and
   keeps it in ``original_data``; a PUT of the same content twice is harmless.
4. A failed push is recorded on the row after the transaction has unwound
   (``push_error``), so the rollback cannot erase it.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import workflow
from .constants import (
    DEFAULT_ISSUE_METHOD,
    DEFAULT_TREE_TYPE,
    DIRECT_LEVEL,
    ISSUE_METHOD_MAP,
    OPEN_STATUSES,
    SAP_ITEM_TYPES,
    SAP_TEXT_LIMIT,
    TREE_TYPE_MAP,
    ApprovalAction,
    BOMChangeKind,
    BOMChangeStatus,
    BOMType,
    LineType,
)
from .models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest
from .permissions import DIRECT_PERMISSION, PUSH_PERMISSION

logger = logging.getLogger(__name__)

SAP_ERRORS = (SAPValidationError, SAPConnectionError, SAPDataError)


class RequestStateError(APIException):
    """400 with ``{"detail": ...}``: the request is not in a state for this."""

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = "bom_change_state"
    default_detail = "This BOM change request cannot do that now."


class TreeAlreadyInSAP(APIException):
    status_code = http_status.HTTP_409_CONFLICT
    default_code = "bom_exists_in_sap"
    default_detail = "SAP already has a BOM for this item."


# ---------------------------------------------------------------------------
# The ProductTrees payload (pushBomToSap, server.js lines 485-519)
# ---------------------------------------------------------------------------


def _tree_type(bom_type: str) -> str:
    return TREE_TYPE_MAP.get((bom_type or "").lower(), DEFAULT_TREE_TYPE)


def _issue_method(value: str) -> str:
    return ISSUE_METHOD_MAP.get(value, DEFAULT_ISSUE_METHOD)


def _quantity(value) -> float:
    # The portal's ``Number(qty) || 1``. Quantities are refused at zero on the
    # way in, so the fallback only guards an imported row.
    return float(value or 0) or 1.0


def build_create_payload(request: BOMChangeRequest, lines) -> dict:
    """POST body for a new tree -- the CREATE branch of ``pushBomToSap`` (lines 488-503).

    Lines are numbered 0.. in their order; a line without a warehouse takes the
    header's; a unit cost above zero goes as the price in INR; a comment is
    kept to SAP_TEXT_LIMIT, and so is the description, as the portal cut them.
    """
    header_warehouse = (request.warehouse or "").strip()
    tree_lines = []
    for index, line in enumerate(lines):
        entry = {
            "ItemCode": line.item_code.strip().upper(),
            "Quantity": _quantity(line.quantity),
            "IssueMethod": _issue_method(line.issue_method),
            "ItemType": SAP_ITEM_TYPES.get(line.item_type, "pit_Item"),
            "VisualOrder": index,
            "PriceList": -1,
        }
        warehouse = (line.warehouse or "").strip() or header_warehouse
        if warehouse:
            entry["Warehouse"] = warehouse
        if line.unit_cost and line.unit_cost > 0:
            entry["Price"] = float(line.unit_cost)
            entry["Currency"] = "INR"
        if (line.comment or "").strip():
            entry["Comment"] = line.comment.strip()[:SAP_TEXT_LIMIT]
        tree_lines.append(entry)

    payload = {
        "TreeCode": request.item_code,
        "TreeType": _tree_type(request.bom_type),
        "Quantity": _quantity(request.quantity),
        "ProductDescription": (request.item_name or "")[:SAP_TEXT_LIMIT],
        "PriceList": -1,
        "ProductTreeLines": tree_lines,
    }
    if header_warehouse:
        payload["Warehouse"] = header_warehouse
    if (request.distribution_rule or "").strip():
        payload["DistributionRule"] = request.distribution_rule.strip()
    if (request.project or "").strip():
        payload["Project"] = request.project.strip()
    return payload


def build_update_payload(request: BOMChangeRequest, lines) -> dict:
    """PUT body replacing a tree -- the UPDATE branch of ``pushBomToSap`` (lines 504-517).

    Kept different from CREATE where the portal was: the header ``Warehouse``
    is always sent (``''`` when empty), the description only when there is one,
    each line keeps the visual order it was given, and lines carry neither a
    price nor a comment.

    One difference from the portal, on purpose: the distribution rule and
    project go too when the request has them. The portal's UPDATE form never
    showed them and its PUT left them out; here they are pre-filled from the
    tree SAP holds, so sending them keeps them rather than risking a PUT that
    resets what it was not given.
    """
    header_warehouse = (request.warehouse or "").strip()
    payload = {
        "TreeType": _tree_type(request.bom_type),
        "Quantity": _quantity(request.quantity),
        "Warehouse": header_warehouse,
        "PriceList": -1,
    }
    if request.item_name:
        payload["ProductDescription"] = request.item_name[:SAP_TEXT_LIMIT]
    if (request.distribution_rule or "").strip():
        payload["DistributionRule"] = request.distribution_rule.strip()
    if (request.project or "").strip():
        payload["Project"] = request.project.strip()

    tree_lines = []
    for index, line in enumerate(lines):
        entry = {
            "ItemCode": line.item_code.strip().upper(),
            "Quantity": _quantity(line.quantity),
            "IssueMethod": _issue_method(line.issue_method),
            "ItemType": SAP_ITEM_TYPES.get(line.item_type, "pit_Item"),
            "VisualOrder": line.visual_order if line.visual_order is not None else index,
            "PriceList": -1,
        }
        warehouse = (line.warehouse or "").strip() or header_warehouse
        if warehouse:
            entry["Warehouse"] = warehouse
        tree_lines.append(entry)
    payload["ProductTreeLines"] = tree_lines
    return payload


# ---------------------------------------------------------------------------
# Who may do what (also the row flags the page's buttons follow)
# ---------------------------------------------------------------------------


def _approved_before(request: BOMChangeRequest, user) -> bool:
    """Has ``user`` approved any level of this request? Uses prefetched approvals."""
    return any(
        approval.action == ApprovalAction.APPROVE and approval.decided_by_id == user.pk
        for approval in request.approvals.all()
    )


def may_cancel(request: BOMChangeRequest, user) -> bool:
    """The person who asked, or a pusher, while nobody has approved it yet.

    The portal let the submitter, a ``sap_adder`` or an ``admin`` cancel, and
    only a PENDING request (server.js lines 526-529).
    """
    if request.status != BOMChangeStatus.PENDING:
        return False
    return (
        request.created_by_id == user.pk
        or user.has_perm(PUSH_PERMISSION)
        or user.has_perm(DIRECT_PERMISSION)
    )


def actions_for(request: BOMChangeRequest, user, levels: int | None = None) -> dict:
    """What ``user`` may do to ``request`` now. ``can_push``: approving writes SAP."""
    levels = levels or workflow.approval_levels()
    right = workflow.right_for(request.status, levels)
    can_decide = right is not None and user.has_perm(right) and not _approved_before(request, user)
    return {
        "can_approve": can_decide,
        "can_reject": can_decide,
        "can_cancel": may_cancel(request, user),
        "can_push": can_decide and workflow.is_final(request.status, levels),
    }


def actionable_statuses(user, levels: int | None = None) -> list[str]:
    """Open statuses at which ``user`` holds the right to decide."""
    levels = levels or workflow.approval_levels()
    return [
        status
        for status in OPEN_STATUSES
        if user.has_perm(workflow.right_for(status, levels))
    ]


def _check_can_decide(request: BOMChangeRequest, user, levels: int) -> None:
    right = workflow.right_for(request.status, levels)
    if right is None:
        raise RequestStateError(
            f"This request is {request.get_status_display().lower()}; there is nothing left to decide."
        )
    if not user.has_perm(right):
        raise PermissionDenied(
            f"This request is waiting for: {workflow.awaiting_label(request.status, levels)}. "
            "You do not hold that right."
        )
    already = BOMChangeApproval.objects.filter(
        request=request, action=ApprovalAction.APPROVE, decided_by=user
    ).exists()
    if already:
        raise RequestStateError(
            "You have already approved this request. Each level must be signed by a different person."
        )


# ---------------------------------------------------------------------------
# Raising a request
# ---------------------------------------------------------------------------

_PREFILL = (
    # request field, key in the tree read from SAP
    ("item_name", "description"),
    ("quantity", "quantity"),
    ("bom_type", "bom_type"),
    ("warehouse", "warehouse"),
    ("distribution_rule", "distribution_rule"),
    ("project", "project"),
)


def _prepare(data: dict, client: SAPClient) -> tuple[dict, dict | None]:
    """The header to save, and (UPDATE) the tree as SAP holds it.

    CREATE: refused with 409 when SAP already has a tree for the item -- the
    push checks again, but there is no point queueing an approval for a BOM
    that cannot be created. UPDATE: the tree must exist; header fields the
    request left out are taken from it.
    """
    item_code = data["item_code"]
    header = {
        "kind": data["kind"],
        "item_code": item_code,
        "item_name": data.get("item_name", ""),
        "quantity": data.get("quantity", 1),
        "bom_type": data.get("bom_type", BOMType.PRODUCTION),
        "warehouse": data.get("warehouse", ""),
        "distribution_rule": data.get("distribution_rule", ""),
        "project": data.get("project", ""),
    }
    if data["kind"] == BOMChangeKind.CREATE:
        if client.product_tree_exists(item_code):
            raise TreeAlreadyInSAP(
                f"SAP already has a BOM for {item_code}. Ask for a change to it instead."
            )
        return header, None

    current = client.get_product_tree(item_code)
    if current is None:
        raise ValidationError({"item_code": [f"SAP has no BOM for {item_code} to change."]})
    for field, key in _PREFILL:
        if field not in data:
            value = current.get(key)
            if field == "bom_type":
                value = value if value in BOMType.values else BOMType.PRODUCTION
            header[field] = value if value is not None else header[field]
    return header, current


def _insert(company, user, header: dict, lines: list[dict], original) -> BOMChangeRequest:
    request = BOMChangeRequest.objects.create(
        company=company,
        created_by=user,
        original_data=original,
        **header,
    )
    BOMChangeLine.objects.bulk_create(
        [
            BOMChangeLine(
                request=request,
                visual_order=index,
                item_type=line.get("item_type", LineType.ITEM),
                item_code=line["item_code"],
                item_name=line.get("item_name", ""),
                quantity=line["quantity"],
                issue_method=line.get("issue_method") or "Manual",
                warehouse=line.get("warehouse", ""),
                unit_cost=line.get("unit_cost") or 0,
                comment=line.get("comment", ""),
            )
            for index, line in enumerate(lines)
        ]
    )
    return request


def create_request(company, user, data: dict) -> BOMChangeRequest:
    """Raise a request; it waits for level 1. ``data`` is the validated serializer data."""
    client = SAPClient(company_code=company.code)
    header, original = _prepare(data, client)
    with transaction.atomic():
        return _insert(company, user, header, data["lines"], original)


def direct_push(company, user, data: dict) -> BOMChangeRequest:
    """Raise a request and write it to SAP at once (the portal admin's direct
    create / update). One transaction: if SAP refuses, no request is left."""
    client = SAPClient(company_code=company.code)
    with transaction.atomic():
        header, original = _prepare(data, client)
        request = _insert(company, user, header, data["lines"], original)
        BOMChangeApproval.objects.create(
            request=request,
            level=DIRECT_LEVEL,
            from_status=request.status,
            action=ApprovalAction.APPROVE,
            decided_by=user,
            remarks=data.get("remarks") or "Direct push to SAP",
        )
        _push(request, user, client)
    return request


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def _locked(company, pk) -> BOMChangeRequest:
    # No select_related: PostgreSQL refuses FOR UPDATE on the nullable side of
    # an outer join, and ``of=("self",)`` locks this row only.
    try:
        return BOMChangeRequest.objects.select_for_update(of=("self",)).get(pk=pk, company=company)
    except BOMChangeRequest.DoesNotExist:
        raise NotFound("BOM change request not found.")


def approve(company, pk, user, remarks: str = "") -> BOMChangeRequest:
    """Approve at the request's current level; the final approval writes SAP."""
    try:
        return _approve(company, pk, user, remarks)
    except (TreeAlreadyInSAP, *SAP_ERRORS) as exc:
        _record_push_failure(company, pk, exc)
        raise


@transaction.atomic
def _approve(company, pk, user, remarks: str) -> BOMChangeRequest:
    request = _locked(company, pk)
    levels = workflow.approval_levels()
    _check_can_decide(request, user, levels)
    from_status = request.status
    BOMChangeApproval.objects.create(
        request=request,
        level=workflow.level_of(from_status, levels),
        from_status=from_status,
        action=ApprovalAction.APPROVE,
        decided_by=user,
        remarks=remarks or "",
    )
    if workflow.is_final(from_status, levels):
        _push(request, user, SAPClient(company_code=request.company.code))
        return request
    request.status = workflow.next_status(from_status, levels)
    request.updated_by = user
    request.save(update_fields=["status", "updated_by", "updated_at"])
    return request


@transaction.atomic
def reject(company, pk, user, remarks: str = "") -> BOMChangeRequest:
    """Reject at the request's current level. Nothing is written to SAP."""
    request = _locked(company, pk)
    levels = workflow.approval_levels()
    _check_can_decide(request, user, levels)
    BOMChangeApproval.objects.create(
        request=request,
        level=workflow.level_of(request.status, levels),
        from_status=request.status,
        action=ApprovalAction.REJECT,
        decided_by=user,
        remarks=remarks or "",
    )
    request.status = BOMChangeStatus.REJECTED
    request.updated_by = user
    request.save(update_fields=["status", "updated_by", "updated_at"])
    return request


@transaction.atomic
def cancel(company, pk, user) -> BOMChangeRequest:
    """Withdraw a request nobody has approved yet (the portal's DELETE)."""
    request = _locked(company, pk)
    allowed = (
        request.created_by_id == user.pk
        or user.has_perm(PUSH_PERMISSION)
        or user.has_perm(DIRECT_PERMISSION)
    )
    if not allowed:
        raise PermissionDenied("Only the person who asked for it, or a BOM pusher, can cancel this request.")
    if request.status != BOMChangeStatus.PENDING:
        raise RequestStateError(
            f"Only a pending request can be cancelled; this one is {request.get_status_display().lower()}."
        )
    now = timezone.now()
    request.status = BOMChangeStatus.CANCELLED
    request.cancelled_at = now
    request.cancelled_by = user
    request.updated_by = user
    request.save(update_fields=["status", "cancelled_at", "cancelled_by", "updated_by", "updated_at"])
    return request


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------


def _push(request: BOMChangeRequest, user, client: SAPClient) -> None:
    """Guards, then the SAP write, then the row. Runs inside the caller's
    transaction with the row locked (or just inserted)."""
    lines = list(BOMChangeLine.objects.filter(request=request).order_by("visual_order", "id"))
    if not lines:
        raise RequestStateError("A BOM needs at least one component.")

    if request.kind == BOMChangeKind.CREATE:
        if client.product_tree_exists(request.item_code):
            raise TreeAlreadyInSAP(
                f"SAP already has a BOM for {request.item_code}, so this new BOM was not written. "
                "If an earlier push got no answer, check that BOM in SAP."
            )
        answer = client.create_product_tree(build_create_payload(request, lines)) or {}
        result = {"tree_code": answer.get("tree_code") or request.item_code, "operation": "CREATED"}
    else:
        current = client.get_product_tree(request.item_code)
        if current is None:
            raise RequestStateError(f"SAP has no BOM for {request.item_code} to change.")
        request.original_data = current
        client.replace_product_tree(request.item_code, build_update_payload(request, lines))
        result = {"tree_code": request.item_code, "operation": "UPDATED"}

    request.status = BOMChangeStatus.SAP_PUSHED
    request.sap_result = result
    request.sap_pushed_at = timezone.now()
    request.sap_pushed_by = user
    request.push_error = ""
    request.push_failed_at = None
    request.updated_by = user
    request.save()
    logger.info(
        "BOM change %s: %s %s in SAP (%s) by %s",
        request.pk, result["operation"].lower(), request.item_code, request.company.code, user.pk,
    )


def _record_push_failure(company, pk, exc) -> None:
    """After the transaction unwound: note why the push failed, for the next person."""
    detail = getattr(exc, "detail", None)
    message = str(detail if detail is not None else exc) or type(exc).__name__
    BOMChangeRequest.objects.filter(pk=pk, company=company).update(
        push_error=message[:2000], push_failed_at=timezone.now()
    )
    logger.warning("BOM change %s: push to SAP failed: %s", pk, message)
