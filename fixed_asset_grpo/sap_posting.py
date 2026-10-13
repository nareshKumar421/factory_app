"""A saved fixed-asset GRPO as a posting the SAP queue can send again.

Kind ``fixed_asset_grpo.post``, registered into ``sap_postings`` by
``FixedAssetGrpoConfig.ready()``. It mirrors ``grpo.sap_posting
.MaterialGRPOHandler`` -- which is not edited and keeps posting raw-material
and finished-goods GRPOs -- and adds the three FA switches in the binding
order (spec D8/D9):

1. **Enabled.** ``FIXED_ASSET_GRPO_ENABLED`` off: the draft is marked waiting
   (QUEUED, by ``update()`` so nobody is told it failed) and the outcome is
   *waiting*. Nothing is read or written. The worker, and a Retry from the SAP
   postings screen, find it switched off again until someone turns it on.
2. **Simulate.** ``FIXED_ASSET_GRPO_SIMULATE`` on:
   ``FixedAssetGRPOService.simulate_saved_grpo`` runs every check (reading
   SAP), writes nothing to SAP, and records the draft as a simulated POSTED
   GRPO. Checked before the guard, so a dry run works on a live server.
3. **Live guard.** ``sap_purchasing.live_guard``: unless
   ``FIXED_ASSET_GRPO_ALLOW_LIVE`` is on, the company's Service Layer database
   and HANA schema must both be ``TEST_``. A refusal marks the draft FAILED and
   is *rejected*; no SAP call is made.

Then exactly the material handler's real send: read back first
(``find_app_posted_grpo``, the "Gate Entry: <no>" stamp plus the same PO
DocEntries), adopt what SAP already holds, otherwise ``post_saved_grpo``. A
read that cannot reach HANA raises ``SAPUnavailable`` and the try *waits*: not
knowing whether the GRPO is there is never read as "it is not".

The person recorded on the post is ``draft.posted_by`` (whoever last saved
or pressed Post), falling back to the queue row's ``created_by``.
"""

from hdbcli import dbapi
from rest_framework.exceptions import ValidationError as DRFValidationError

from grpo.models import GRPOPosting, GRPOStatus
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings.services import Outcome

from . import flags
from .constants import FA_ENTRY_TYPE, FRONTEND_BASE, KIND

DISABLED_MESSAGE = "Fixed-asset GRPO posting is switched off; this waits until it is on."


def title_for(draft: GRPOPosting, po_numbers) -> str:
    return f"FA GRPO {draft.vehicle_entry.entry_no} (PO {', '.join(po_numbers)})"


def draft_link(draft: GRPOPosting) -> str:
    return f"{FRONTEND_BASE}/preview/{draft.vehicle_entry_id}?draft={draft.id}"


def posted_link(posting_id: int) -> str:
    return f"{FRONTEND_BASE}/history/{posting_id}"


def link_for(draft: GRPOPosting) -> str:
    """Where the queue row should point: history once posted, else the preview."""
    if draft.status == GRPOStatus.POSTED:
        return posted_link(draft.id)
    return draft_link(draft)


def _error_text(exc) -> str:
    if isinstance(exc, DRFValidationError):
        detail = exc.detail
        if isinstance(detail, dict):
            return "; ".join(f"{key}: {value}" for key, value in detail.items())
        return str(detail)
    return str(exc) or exc.__class__.__name__


def _document(draft, receipts, **fields):
    """The attempt log's line: what was sent, and what came of it."""
    return {
        "reference": f"Gate Entry: {draft.vehicle_entry.entry_no}",
        "invoices": ", ".join(pr.po_number for pr in receipts),
        "card_code": receipts[0].supplier_code if receipts else "",
        "payload": draft.request_payload or {},
        **fields,
    }


class FixedAssetGRPOHandler:
    kind = KIND

    def send(self, posting):
        from .notifications import notify_fixed_asset_grpo_posted
        from .services.grpo import SIMULATED_MESSAGE, FixedAssetGRPOService
        from .services.state import is_simulated, record_fa_post

        draft = (
            GRPOPosting.objects.select_related("vehicle_entry__company")
            .filter(pk=posting.source_id)
            .first()
        )
        if draft is None:
            return Outcome.rejected(
                "The saved GRPO no longer exists: it was posted or deleted from the app."
            )
        if (draft.vehicle_entry.entry_type or "") != FA_ENTRY_TYPE:
            return Outcome.rejected(
                "This GRPO is not for a fixed-asset gate entry; it is posted from its own screen."
            )
        if draft.status == GRPOStatus.POSTED:
            simulated = is_simulated(draft)
            return Outcome.posted(
                SIMULATED_MESSAGE if simulated else f"SAP GRPO {draft.sap_doc_num}",
                result={
                    "doc_nums": [] if simulated else [str(draft.sap_doc_num)],
                    "grpo_posting_id": draft.id,
                    "simulated": simulated,
                },
                link=posted_link(draft.id),
                context={"posting": draft, "simulated": simulated},
            )

        # 1. The kill switch, before anything is read.
        if not flags.enabled():
            FixedAssetGRPOService.mark_grpo_waiting_for_sap(draft.id, DISABLED_MESSAGE)
            return Outcome.waiting(DISABLED_MESSAGE, context={"draft_id": draft.id})

        company_code = draft.vehicle_entry.company.code
        service = FixedAssetGRPOService(company_code)
        receipts = service.draft_po_receipts(draft)
        user = draft.posted_by or posting.created_by

        try:
            # 2. Simulate: every check, SAP read but never written.
            if flags.simulate():
                simulated = service.simulate_saved_grpo(grpo_posting_id=draft.id, user=user)
                return Outcome.posted(
                    SIMULATED_MESSAGE,
                    result={"doc_nums": [], "grpo_posting_id": simulated.id, "simulated": True},
                    detail={"documents": [_document(draft, receipts, outcome="simulated")]},
                    link=posted_link(simulated.id),
                    context={"posting": simulated, "simulated": True},
                )

            # 3. The live-SAP guard, before any SAP call of a real send.
            try:
                flags.require_live_allowed(company_code)
            except (ValueError, SAPValidationError) as exc:
                message = _error_text(exc)
                GRPOPosting.objects.filter(id=draft.id).update(
                    status=GRPOStatus.FAILED, error_message=message
                )
                return Outcome.rejected(
                    message,
                    detail={"documents": [_document(draft, receipts, outcome="refused", error=message)]},
                    context={"draft_id": draft.id, "error": exc},
                )

            found = service.find_app_posted_grpo(draft)
            if found:
                adopted = service.adopt_app_posted_grpo(draft, found, user)
                record_fa_post(adopted, user=user, simulated=False)
                notify_fixed_asset_grpo_posted(adopted)
                return Outcome.posted(
                    f"SAP GRPO {adopted.sap_doc_num} (already in SAP; not posted twice)",
                    result={"doc_nums": [str(adopted.sap_doc_num)], "grpo_posting_id": adopted.id,
                            "simulated": False},
                    detail={"documents": [_document(draft, receipts, outcome="already in SAP",
                                                    doc_num=str(adopted.sap_doc_num))]},
                    link=posted_link(adopted.id),
                    context={"posting": adopted, "simulated": False},
                )
            posted = service.post_saved_grpo(grpo_posting_id=draft.id, user=user)
        except (SAPConnectionError, dbapi.Error) as exc:
            message = f"SAP could not be reached ({exc}); nothing was posted"
            FixedAssetGRPOService.mark_grpo_waiting_for_sap(draft.id, message)
            return Outcome.waiting(
                message,
                detail={"documents": [_document(draft, receipts, outcome="failed", error=str(exc))]},
                context={"draft_id": draft.id},
            )
        except (ValueError, SAPValidationError, SAPDataError, DRFValidationError) as exc:
            # SAP -- or the app's own last check -- said no. That repeats until
            # someone changes something. post_saved_grpo / simulate_saved_grpo
            # already kept the draft FAILED; a payload that no longer validates
            # did not reach them, so mark it here.
            message = _error_text(exc)
            GRPOPosting.objects.filter(
                id=draft.id, status__in=[GRPOStatus.DRAFT, GRPOStatus.QUEUED, GRPOStatus.FAILED]
            ).update(status=GRPOStatus.FAILED, error_message=message)
            return Outcome.rejected(
                message,
                detail={"documents": [_document(draft, receipts, outcome="failed", error=message)]},
                context={"draft_id": draft.id, "error": exc},
            )
        except Exception as exc:
            # A bug, not SAP: the draft must still survive it, marked failed.
            GRPOPosting.objects.filter(id=draft.id).update(
                status=GRPOStatus.FAILED, error_message=f"Unexpected error: {exc}"
            )
            raise

        record_fa_post(posted, user=user, simulated=False)
        notify_fixed_asset_grpo_posted(posted)
        return Outcome.posted(
            f"SAP GRPO {posted.sap_doc_num}",
            result={"doc_nums": [str(posted.sap_doc_num)], "grpo_posting_id": posted.id,
                    "simulated": False},
            detail={"documents": [_document(draft, receipts, outcome="posted",
                                            doc_num=str(posted.sap_doc_num))]},
            link=posted_link(posted.id),
            context={"posting": posted, "simulated": False},
        )
