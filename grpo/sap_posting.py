"""A saved material GRPO -- raw material or bought-in finished goods -- as a
posting the SAP queue can send again.

A try is ``GRPOService.post_saved_grpo``: the draft already holds the payload
and the attachments, and posting it deletes the draft in favour of a new
POSTED row. Before every try SAP is asked whether the app already posted this
truck's GRPO (``find_app_posted_grpo``), so a retry after a timeout that SAP
committed anyway records that document instead of posting a second one.

Every SAP read and write happens here, at send time. Queuing the GRPO needs
nothing from SAP -- saving the draft is local -- so a GRPO can be taken while
the Service Layer, HANA or both are down.
"""

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings.services import Outcome

from .models import GRPOPosting, GRPOStatus

KIND = "grpo.material"


def title_for(draft: GRPOPosting, po_numbers) -> str:
    return f"GRPO {draft.vehicle_entry.entry_no} (PO {', '.join(po_numbers)})"


def _entry_type(draft: GRPOPosting) -> str:
    return getattr(draft.vehicle_entry, "entry_type", "") or "RAW_MATERIAL"


def draft_link(draft: GRPOPosting) -> str:
    # Bought-in finished goods are received on their own screen.
    if _entry_type(draft) == "FINISHED_GOODS":
        return f"/warehouse/grpo/fg/preview/{draft.vehicle_entry_id}"
    return f"/warehouse/grpo/material/preview/{draft.vehicle_entry_id}?draft={draft.id}"


def posted_link(posting_id: int) -> str:
    return f"/warehouse/grpo/material/history/{posting_id}"


def _document(draft, receipts, **fields):
    """The attempt log's line: what was sent, and what came of it."""
    return {
        "reference": f"Gate Entry: {draft.vehicle_entry.entry_no}",
        "invoices": ", ".join(pr.po_number for pr in receipts),
        "card_code": receipts[0].supplier_code if receipts else "",
        "payload": draft.request_payload or {},
        **fields,
    }


class MaterialGRPOHandler:
    kind = KIND

    def send(self, posting):
        from .services import GRPOService

        draft = (
            GRPOPosting.objects.select_related("vehicle_entry__company")
            .filter(pk=posting.source_id)
            .first()
        )
        if draft is None:
            return Outcome.rejected(
                "The saved GRPO no longer exists: it was posted or deleted from the app."
            )
        if draft.status == GRPOStatus.POSTED:
            return Outcome.posted(
                f"SAP GRPO {draft.sap_doc_num}",
                result={"doc_nums": [str(draft.sap_doc_num)], "grpo_posting_id": draft.id},
                link=posted_link(draft.id),
            )

        # Posted under its own gate entry's rules: a bought-in finished-goods
        # GRPO has no QC slip, and the raw-material checks would refuse it.
        service = GRPOService(
            company_code=draft.vehicle_entry.company.code, entry_type=_entry_type(draft)
        )
        receipts = service.draft_po_receipts(draft)
        user = posting.created_by
        try:
            found = service.find_app_posted_grpo(draft)
            if found:
                adopted = service.adopt_app_posted_grpo(draft, found, user)
                return Outcome.posted(
                    f"SAP GRPO {adopted.sap_doc_num} (already in SAP; not posted twice)",
                    result={"doc_nums": [str(adopted.sap_doc_num)], "grpo_posting_id": adopted.id},
                    detail={"documents": [_document(draft, receipts, outcome="already in SAP",
                                                    doc_num=str(adopted.sap_doc_num))]},
                    link=posted_link(adopted.id),
                    context={"posting": adopted},
                )
            posted = service.post_saved_grpo(grpo_posting_id=draft.id, user=user)
        except (SAPConnectionError, dbapi.Error) as exc:
            message = f"SAP could not be reached ({exc}); nothing was posted"
            GRPOService.mark_grpo_waiting_for_sap(draft.id, message)
            return Outcome.waiting(
                message,
                detail={"documents": [_document(draft, receipts, outcome="failed", error=str(exc))]},
                context={"draft_id": draft.id},
            )
        except (ValueError, SAPValidationError, SAPDataError) as exc:
            # SAP -- or the app's own last check, like the PO's open quantity --
            # said no. That repeats until someone changes something.
            return Outcome.rejected(
                str(exc),
                detail={"documents": [_document(draft, receipts, outcome="failed", error=str(exc))]},
                context={"draft_id": draft.id, "error": exc},
            )
        except Exception as exc:
            # A bug, not SAP: the draft must still survive it, marked failed
            # rather than left looking untried. The queue logs it as refused.
            GRPOPosting.objects.filter(id=draft.id).update(
                status=GRPOStatus.FAILED, error_message=f"Unexpected error: {exc}"
            )
            raise

        return Outcome.posted(
            f"SAP GRPO {posted.sap_doc_num}",
            result={"doc_nums": [str(posted.sap_doc_num)], "grpo_posting_id": posted.id},
            detail={"documents": [_document(draft, receipts, outcome="posted",
                                            doc_num=str(posted.sap_doc_num))]},
            link=posted_link(posted.id),
            context={"posting": posted},
        )
