"""A goods return's A/R Returns, as a posting the SAP queue can send again.

A try *is* a receive. ``GoodsReturnService.receive`` already skips the bills SAP
has taken and asks SAP, before every post, whether a document with the note's
reference exists -- so a retry after SAP was down, after a timeout that SAP
committed anyway, or after a worker died mid-send, adopts what is there instead
of posting it twice.
"""

from hdbcli import dbapi
from rest_framework.exceptions import PermissionDenied

from sap_client.exceptions import SAPConnectionError
from sap_postings.services import Outcome

from .models import GoodsReturn

KIND = "goods_return.receive"


def title_for(gr: GoodsReturn) -> str:
    bills = ", ".join(
        ref.sap_invoice_doc_num for ref in gr.active_invoice_refs if ref.sap_invoice_doc_num
    )
    return f"Goods return {gr.entry_no}" + (f" (invoice {bills})" if bills else "")


def link_for(gr: GoodsReturn) -> str:
    return f"/returns/customer/{gr.pk}"


def _doc_nums(gr: GoodsReturn) -> list:
    nums = [ref.sap_gr_doc_num for ref in gr.invoice_refs.all() if ref.sap_gr_doc_num]
    if not nums and gr.sap_gr_doc_num:
        nums = [gr.sap_gr_doc_num]
    return sorted(set(nums))


class ReceiveHandler:
    kind = KIND

    def send(self, posting):
        from .services import GoodsReturnService, NothingPostedError

        gr = GoodsReturn.objects.select_related("company").get(pk=posting.source_id)
        service = GoodsReturnService(company=gr.company)
        user = posting.created_by
        warehouse = posting.params.get("warehouse_code", "")
        try:
            gr = service.receive(
                gr.pk,
                user,
                warehouse,
                [gr.company_id],
                grouping=posting.params.get("grouping"),
            )
        except NothingPostedError as exc:
            # The receive rolled back; what SAP said about each bill is kept.
            GoodsReturnService.record_posting_errors(exc.refused, user)
            detail = {"documents": exc.log}
            if exc.unreachable:
                GoodsReturnService.mark_waiting_for_sap(gr.pk, user, warehouse)
                return Outcome.waiting(str(exc), detail=detail, context={"gr_id": gr.pk})
            return Outcome.rejected(str(exc), detail=detail, context={"gr_id": gr.pk})
        except (SAPConnectionError, dbapi.Error) as exc:
            # A read before any post (branch, tax codes, the duplicate check)
            # found SAP down: nothing was sent, so it waits like any other.
            GoodsReturnService.mark_waiting_for_sap(gr.pk, user, warehouse)
            return Outcome.waiting(
                f"SAP could not be reached ({exc}); nothing was posted",
                context={"gr_id": gr.pk},
            )
        except (ValueError, PermissionDenied) as exc:
            return Outcome.rejected(str(exc), context={"gr_id": gr.pk})

        detail = {"documents": getattr(gr, "posting_log", [])}
        failures = getattr(gr, "posting_failures", None) or []
        context = {"gr_id": gr.pk, "failures": failures}
        if failures:
            message = GoodsReturnService._posting_failure_message(failures)
            # Some bills posted; the rest either wait for SAP or need a person.
            if getattr(gr, "sap_unreachable", False):
                return Outcome.waiting(message, detail=detail, context=context)
            return Outcome.rejected(message, detail=detail, context=context)

        nums = _doc_nums(gr)
        return Outcome.posted(
            (
                f"SAP Return{'s' if len(nums) > 1 else ''} {', '.join(nums)}"
                if nums
                else "Posted to SAP"
            ),
            result={"doc_nums": nums},
            detail=detail,
            context=context,
        )
