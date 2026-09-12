"""Add inventory-transfer drafts SAP's approval procedure left unposted.

A transfer keyed in the SAP client on an approval-covered route is saved as a
**draft**, not a document. Approving it clears the approval and moves nothing;
somebody must still open the draft and press **Add**. Nobody always does — 33
approved drafts were sitting unadded across the three companies when this was
written, the oldest 612 days old — and until then the stock has not moved and
no ``OWTR`` exists, so the move is invisible to every other queue in this app.

This is the app's Add button. Three things make it different from its sibling
``sap_transfer_post_service``, which posts against a transfer *request*:

* **Nothing is chosen here.** That flow builds a document from open request
  quantities, so it takes a quantity per line; this one posts a document SAP
  already holds, exactly as authored — items, quantities, warehouses and the
  operator's own batch allocations. Editing belongs in SAP, on the draft.
* **Cross-branch is fine.** A branch-crossing *request* would have to be built
  as two legs through an in-transit warehouse, which is why that flow refuses
  it. A draft already says what it is, in-transit leg and all (``BH-VG`` →
  ``DL-INT`` is one of the waiting ones), so adding it is just adding it.
* **The add can fail where the draft did not.** ``SBO_SP_TransactionNotification``
  runs on the add and never ran at draft time, so SAP's refusal is surfaced
  verbatim rather than translated.
"""

import logging

from django.utils import timezone

from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from ..models_sap_draft_post import SapTransferDraftPost
from .warehouse_scope import assert_manages

logger = logging.getLogger(__name__)


class SapTransferDraftError(Exception):
    """The draft cannot be added, and the operator can act on why."""


class SapTransferDraftService:
    """List approved-but-unposted transfer drafts, and add them in SAP."""

    def __init__(self, company_code: str, user):
        self.company_code = company_code
        self.user = user
        self.client = SAPClient(company_code=company_code)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_awaiting_add(self, limit: int = 100) -> list[dict]:
        """Approved drafts whose stock is still sitting at the source.

        Company-wide, like the approval queue and the awaiting-transfer list
        beside it: a draft's two warehouses have different managers and the
        person who keyed it is often neither, so scoping the list to the
        caller's warehouses would hide rows nobody could then find. Whether this
        caller may add each one is reported per row instead.
        """
        rows = self.client.list_unposted_transfer_drafts(limit=limit)
        manageable = self._manageable_warehouses()

        out = []
        for row in rows:
            sources = self._source_warehouses(row)
            may_add = manageable is None or sources.issubset(manageable)
            problems = [
                line for line in row["lines"] if line.get("batches_missing")
            ]
            out.append({
                **row,
                # Adding moves stock OUT, so it is the sending side's call.
                "can_post": bool(may_add),
                "blocked_reason": self._blocked_reason(may_add, sources, manageable),
                # Named separately from `can_post`: these are SAP's problem with
                # the draft, not this caller's permissions, and both can be true.
                "warnings": self._warnings(row, problems),
            })
        return out

    def _warnings(self, row: dict, missing_batches: list) -> list[str]:
        warnings = []
        short = [line for line in row["lines"] if line.get("short")]
        if short:
            names = ", ".join(sorted({line["item_code"] for line in short}))
            warnings.append(
                f"{row['from_warehouse']} no longer holds enough stock for "
                f"{names}. SAP will refuse the add until it does."
            )
        if missing_batches:
            names = ", ".join(sorted({line["item_code"] for line in missing_batches}))
            warnings.append(
                f"{names} is batch-managed but the draft carries no batch "
                "allocation. Set it on the draft in SAP first."
            )
        return warnings

    @staticmethod
    def _blocked_reason(may_add: bool, sources: set, manageable):
        if may_add:
            return None
        unmanaged = sorted(sources - (manageable or set()))
        return (
            f"You do not manage {', '.join(unmanaged)}, "
            f"{'the warehouse' if len(unmanaged) == 1 else 'the warehouses'} "
            "the stock leaves."
        )

    @staticmethod
    def _source_warehouses(draft: dict) -> set:
        """Every warehouse this draft takes stock out of, upper-cased.

        The lines carry their own source and genuinely differ from the header on
        multi-source documents, so the permission check follows the lines and
        falls back to the header only for a line that names none.
        """
        header = (draft.get("from_warehouse") or "").strip().upper()
        sources = {
            (line.get("from_warehouse") or header).strip().upper()
            for line in draft.get("lines") or []
        }
        sources.discard("")
        return sources or ({header} if header else set())

    def _manageable_warehouses(self):
        """Upper-cased warehouse codes the caller manages, or None if unrestricted."""
        from warehouse.models_manager import UserWarehouse

        if getattr(self.user, "is_superuser", False):
            return None
        return {
            code.strip().upper()
            for code in UserWarehouse.objects.filter(
                user=self.user, company__code=self.company_code, is_active=True
            ).values_list("warehouse_code", flat=True)
            if code
        }

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------

    def post_draft(self, draft_entry: int) -> dict:
        """Add draft ``draft_entry`` in SAP, so its stock finally moves."""
        draft = self._load_draft(int(draft_entry))

        # Before the state checks, not after: adding a draft CLOSES it, so an
        # already-added one would otherwise be refused as "closed" when what
        # the operator needs is the number of the transfer that already exists.
        already = self.client.stock_transfer_for_draft(int(draft_entry))
        if already:
            raise SapTransferDraftError(
                f"That draft was already added in SAP as transfer "
                f"{already.get('doc_num') or already['doc_entry']}. "
                "Nothing more to post."
            )
        self._assert_addable(draft)

        assert_manages(
            self.user,
            self.company_code,
            sorted(self._source_warehouses(draft)),
            action="add a transfer out of this warehouse",
        )

        try:
            self.client.add_stock_transfer_draft(int(draft_entry))
        except SAPConnectionError as exc:
            # A timeout does not roll SAP back. Read the draft's document back
            # before reporting a failure the operator would otherwise retry.
            posted = self._posted_after_timeout(draft_entry)
            if posted:
                logger.warning(
                    "Transfer draft %s timed out but SAP had added it as %s",
                    draft_entry, posted.get("doc_num") or posted["doc_entry"],
                )
                return self._record_success(draft, posted, timed_out=True)
            self._record_failure(draft, exc)
            raise
        except (SAPValidationError, SAPDataError) as exc:
            self._record_failure(draft, exc)
            raise

        posted = self.client.stock_transfer_for_draft(int(draft_entry))
        return self._record_success(draft, posted)

    def _load_draft(self, draft_entry: int) -> dict:
        """The draft, refused unless it is an inventory transfer at all."""
        draft = self.client.get_transfer_draft(draft_entry)
        if draft is None:
            raise SapTransferDraftError(f"Draft {draft_entry} was not found in SAP.")
        if not draft["is_transfer"]:
            raise SapTransferDraftError(
                f"Draft {draft_entry} is not an inventory transfer "
                f"(SAP object {draft['obj_type']}), so it cannot be added here."
            )
        return draft

    @staticmethod
    def _assert_addable(draft: dict) -> None:
        """Refuse, by name, anything SAP would not let be added."""
        if draft["cancelled"]:
            raise SapTransferDraftError("That draft is cancelled in SAP.")
        if not draft["is_open"]:
            # DocStatus 'C' is how SAP closes a draft once it has been added.
            raise SapTransferDraftError(
                "That draft is already closed in SAP — it has been added, or "
                "removed, since this page was loaded."
            )
        if not draft["is_approved"]:
            raise SapTransferDraftError(
                f"That draft is {draft['approval_label']} in SAP, so it cannot "
                "be added. Only an approved draft can be."
            )
        if not draft["lines"]:
            raise SapTransferDraftError("That draft has no item lines.")

    def _posted_after_timeout(self, draft_entry: int):
        """Re-read the draft's document, swallowing a second SAP failure.

        Called only on the timeout path, where the honest answer when SAP
        cannot be reached twice is the original "check SAP" error.
        """
        try:
            return self.client.stock_transfer_for_draft(int(draft_entry))
        except (SAPConnectionError, SAPDataError):
            return None

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _record_success(self, draft: dict, posted, timed_out: bool = False) -> dict:
        audit = self._audit(
            draft,
            result=SapTransferDraftPost.RESULT_POSTED,
            doc_entry=(posted or {}).get("doc_entry"),
            doc_num=(posted or {}).get("doc_num"),
        )
        logger.info(
            "Transfer draft %s added in SAP as %s by %s",
            draft["draft_entry"],
            (posted or {}).get("doc_num") or (posted or {}).get("doc_entry"),
            getattr(self.user, "email", self.user),
        )
        return {
            "draft_entry": draft["draft_entry"],
            "doc_entry": (posted or {}).get("doc_entry"),
            "doc_num": (posted or {}).get("doc_num"),
            "from_warehouse": draft["from_warehouse"],
            "to_warehouse": draft["to_warehouse"],
            "lines_moved": len(draft["lines"]),
            # True when SAP had committed it but did not answer in time, so the
            # page can say the add landed on the read-back rather than the call.
            "confirmed_by_readback": bool(timed_out),
            "posted_at": timezone.now().isoformat(),
            "audit_id": audit.id if audit else None,
        }

    def _record_failure(self, draft: dict, exc: Exception) -> None:
        self._audit(
            draft,
            result=SapTransferDraftPost.RESULT_FAILED,
            error_message=str(exc),
        )

    def _audit(self, draft: dict, **fields):
        """Write the audit row, never failing the add because of it."""
        from company.models import Company

        try:
            company = Company.objects.get(code=self.company_code)
            return SapTransferDraftPost.objects.create(
                company=company,
                draft_entry=draft["draft_entry"],
                draft_doc_num=draft.get("doc_num"),
                from_warehouse=draft.get("from_warehouse") or "",
                to_warehouse=draft.get("to_warehouse") or "",
                line_count=len(draft.get("lines") or []),
                created_by=self.user if getattr(self.user, "pk", None) else None,
                **fields,
            )
        except Exception:
            logger.exception(
                "Could not write the audit row for transfer draft %s",
                draft.get("draft_entry"),
            )
            return None
