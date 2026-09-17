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
from decimal import Decimal

from django.utils import timezone

from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from ..models_sap_draft_post import SapTransferDraftPost
from .warehouse_scope import assert_manages_either_side

logger = logging.getLogger(__name__)


def _qty(value) -> str:
    """A quantity as a person reads it: 1,620 rather than 1620.000000."""
    try:
        number = Decimal(str(value or 0))
    except (ArithmeticError, ValueError):
        return str(value)
    whole = number.to_integral_value()
    if number == whole:
        return f"{whole:,f}".split(".")[0]
    return f"{number.normalize():,f}"


def _some(parts: list, limit: int = 3, separator: str = "; ") -> str:
    """The first few of a list, and how many were left unsaid.

    A 29-line draft can be short on all 29, each on several batches. The point
    of the message is that the operator recognises the problem, not that they
    read every instance of it in one sentence.
    """
    parts = list(parts)
    if len(parts) <= limit:
        return separator.join(parts)
    rest = len(parts) - limit
    return separator.join(parts[:limit]) + f", and {rest} more"


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
            sources, destinations = self._sides(row)
            may_add = manageable is None or (
                (sources and sources.issubset(manageable))
                or (destinations and destinations.issubset(manageable))
            )
            warnings = self._warnings(row)
            out.append({
                **row,
                # Either side may release it: the draft is already written, and
                # the manager waiting for the stock is as entitled to add it as
                # the one sending it.
                "can_post": bool(may_add),
                "blocked_reason": self._blocked_reason(
                    may_add, sources, destinations
                ),
                # Named separately from `can_post`: these are SAP's problem with
                # the draft, not this caller's permissions, and both can be true.
                "warnings": warnings,
                # What the page hangs the Add button on. Kept as its own field
                # rather than inferred from `warnings` being non-empty, because
                # a warning worth reading is not the same thing as a refusal
                # that is certain — and only the second should take the button
                # away.
                "will_be_refused": self._will_be_refused(row),
            })
        return out

    # ------------------------------------------------------------------
    # What SAP will say
    # ------------------------------------------------------------------
    #
    # Read before the add rather than after it. Every message below names a
    # refusal SAP is certain to give, in the words of the thing that has to
    # change, because the operator is the one who has to change it: the first
    # live use of this page was five identical presses of Add against a draft
    # whose stock had left the warehouse two months earlier, each answered with
    # a raw "10001153 - Insufficient quantity for item FG0000296 with batch
    # LS1103". The draft was a duplicate of a move already made to another
    # warehouse. Nothing about that was discoverable from the page.

    def _warnings(self, row: dict) -> list[str]:
        """Everything SAP will refuse this draft for, worst first."""
        lines = row["lines"]
        return [
            message
            for message in (
                self._gone_warning(row, lines),
                self._short_warning(row, lines),
                self._batch_warning(lines),
                self._unallocated_warning(lines),
                self._partial_allocation_warning(lines),
            )
            if message
        ]

    def _will_be_refused(self, row: dict) -> bool:
        """True when the add cannot succeed, so the page should not offer it.

        Deliberately not enforced in :meth:`post_draft`: SAP is the authority on
        its own stock, this is a read taken seconds earlier, and a page that
        refuses what SAP would have accepted is worse than one that lets an
        operator insist. The button is put out of the way, not removed.
        """
        return any(
            line.get("short")
            or line.get("batches_missing")
            or line.get("allocation_partial")
            or line.get("batches_short")
            for line in row["lines"]
        )

    @classmethod
    def _gone_warning(cls, row: dict, lines: list):
        """The decisive one: the source warehouse is empty of the item.

        A draft this old is usually not short — it is *stale*: the quantity left
        the warehouse whole on a later document, so no amount of waiting or
        retrying will post it. Where that document can be named, it is, because
        it is what tells the operator whether the move has already been made
        (remove this draft) or the stock needs bringing back (re-key it from
        where the stock now is).
        """
        gone = [line for line in lines if line.get("source_empty")]
        if not gone:
            return None
        names = cls._items(gone)
        where = row["from_warehouse"] or "the source warehouse"
        message = (
            f"{where} holds none of {names} any more, so this draft can no "
            "longer be added."
        )
        issue = cls._latest_issue(gone)
        if issue:
            message += (
                f" The last stock to leave was {_qty(issue['quantity'])} on "
                f"{issue['doc_type']} {issue['doc_num']}, {issue['doc_date']}."
            )
        return message + (
            " If the move was already made another way, remove the draft in "
            "SAP; if it still has to happen, re-key it from the warehouse that "
            "holds the stock now."
        )

    @classmethod
    def _short_warning(cls, row: dict, lines: list):
        """Short but not empty — this one can come right on its own."""
        short = [
            line for line in lines
            if line.get("short") and not line.get("source_empty")
        ]
        if not short:
            return None
        where = row["from_warehouse"] or "the source warehouse"
        detail = _some(
            f"{line['item_code']} needs {_qty(line['quantity'])}, "
            f"{_qty(line['source_stock'])} there"
            for line in short
        )
        return (
            f"{where} no longer holds enough stock: {detail}. SAP will refuse "
            "the add until it does."
        )

    @staticmethod
    def _batch_warning(lines: list):
        """SAP checks the allocated BATCH, not the item total.

        This is the refusal that cannot be seen from the quantities on screen: a
        line can be comfortably covered in ``OITW`` and still name a batch that
        has since been moved or consumed.
        """
        faults = [
            (line["item_code"], batch)
            for line in lines
            for batch in line.get("batches_short") or []
        ]
        if not faults:
            return None
        detail = _some(
            f"batch {batch['batch']} of {item} holds "
            f"{_qty(batch['in_stock'])} of the {_qty(batch['allocated'])} "
            "allocated"
            for item, batch in faults
        )
        return (
            f"SAP refuses per batch, and {detail}. The draft has to be "
            "re-allocated to batches that still exist, in SAP."
        )

    @classmethod
    def _unallocated_warning(cls, lines: list):
        missing = [line for line in lines if line.get("batches_missing")]
        if not missing:
            return None
        return (
            f"{cls._items(missing)} is batch-managed but the draft carries no "
            "batch allocation. Set it on the draft in SAP first."
        )

    @classmethod
    def _partial_allocation_warning(cls, lines: list):
        partial = [line for line in lines if line.get("allocation_partial")]
        if not partial:
            return None
        detail = _some(
            f"{line['item_code']} allocates "
            f"{_qty(line['allocated_quantity'])} of {_qty(line['quantity'])}"
            for line in partial
        )
        return (
            f"Not every piece is allocated to a batch: {detail}. SAP needs the "
            "whole line allocated before it will add it."
        )

    @staticmethod
    def _items(lines: list) -> str:
        return _some(sorted({line["item_code"] for line in lines}), separator=", ")

    @staticmethod
    def _latest_issue(lines: list):
        """The most recent outgoing document across the given lines."""
        issues = [line["last_issue"] for line in lines if line.get("last_issue")]
        issues = [i for i in issues if i.get("doc_num")]
        if not issues:
            return None
        return max(issues, key=lambda i: (i.get("doc_date") or "", i["doc_num"]))

    @staticmethod
    def _blocked_reason(may_add: bool, sources: set, destinations: set):
        """Why this caller cannot add it — naming BOTH sides they could have.

        Whichever side they get assigned unblocks the row, so both are named:
        "you do not manage BH-PF" alone sends an administrator to grant the
        sending side when the receiving one is usually the right answer.
        """
        if may_add:
            return None
        sides = []
        if sources:
            sides.append(f"{', '.join(sorted(sources))} (out of)")
        if destinations:
            sides.append(f"{', '.join(sorted(destinations))} (into)")
        return (
            "You manage neither side of this transfer — "
            + " nor ".join(sides)
            + ". Managing either one is enough to add it."
        )

    @classmethod
    def _sides(cls, draft: dict) -> tuple[set, set]:
        """The warehouses this draft takes stock out of, and puts it into.

        Both from the lines, which carry their own pair and genuinely differ
        from the header on a multi-warehouse document; the header is the
        fallback only for a line that names none.
        """
        return (
            cls._line_warehouses(draft, "from_warehouse"),
            cls._line_warehouses(draft, "to_warehouse"),
        )

    @staticmethod
    def _line_warehouses(draft: dict, field: str) -> set:
        header = (draft.get(field) or "").strip().upper()
        codes = {
            (line.get(field) or header).strip().upper()
            for line in draft.get("lines") or []
        }
        codes.discard("")
        return codes or ({header} if header else set())

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

        sources, destinations = self._sides(draft)
        assert_manages_either_side(
            self.user,
            self.company_code,
            sorted(sources),
            sorted(destinations),
            action="add this transfer",
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
