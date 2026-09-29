"""A short dispatch's A/R Return, as a posting the SAP queue can send again.

A try is ``ShortDispatchService._post_return``: every SAP read it needs (branch,
variety, costs, tax codes, place of supply) happens then, at send time, and it
asks SAP for a Return carrying the entry's reference before posting. The
reference names the entry, and the entry is kept while it waits, so a retry
after a timeout that SAP committed anyway finds that Return instead of posting
a second one nobody can cancel.
"""

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings.services import Outcome

from .models import ShortDispatch, ShortDispatchStatus

KIND = "short_dispatch.post"


def title_for(entry: ShortDispatch) -> str:
    invoice = entry.sap_invoice_doc_num or str(entry.sap_invoice_doc_entry)
    return f"Short dispatch {entry.entry_no} (invoice {invoice})"


def link_for(entry: ShortDispatch) -> str:
    return f"/warehouse/short-dispatch/{entry.pk}"


class ShortDispatchHandler:
    kind = KIND

    def send(self, posting):
        from .services import ShortDispatchService

        entry = (
            ShortDispatch.objects.select_related("company")
            .prefetch_related("lines")
            .filter(pk=posting.source_id)
            .first()
        )
        if entry is None:
            return Outcome.rejected("The short dispatch was withdrawn, so there is nothing to post.")
        if entry.status == ShortDispatchStatus.POSTED:
            return Outcome.posted(
                f"SAP Return {entry.sap_return_doc_num}",
                result={"doc_nums": [entry.sap_return_doc_num]},
            )

        service = ShortDispatchService(entry.company)
        try:
            service._post_return(entry, posting.created_by)
        except (SAPConnectionError, dbapi.Error) as exc:
            message = f"SAP could not be reached ({exc}); nothing was posted"
            ShortDispatchService.mark_waiting(entry.pk, message)
            return Outcome.waiting(message)
        except (ValueError, SAPValidationError, SAPDataError) as exc:
            # Refused. Kept as REFUSED for the worker's tries; create_and_post
            # withdraws an entry refused while its operator is still at the form.
            ShortDispatchService.mark_refused(entry.pk, str(exc))
            return Outcome.rejected(str(exc))

        return Outcome.posted(
            f"SAP Return {entry.sap_return_doc_num}",
            result={"doc_nums": [entry.sap_return_doc_num]},
        )
