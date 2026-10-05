"""A bill summary's invoice stamp, as a posting the SAP queue can send again.

One kind for both directions, as ``BillSummaryService.post_to_sap`` always was:
an approved sheet stamps its invoice (dispatch date, line quantities, bilty...),
a cancelled one takes the stamp back off. A resend is safe -- the stamp reads
what SAP already holds and keeps it, so sending it twice writes the same values
-- which is what lets a try that timed out simply be tried again.
"""

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_postings.services import Outcome

from .models_bill_summary import BillSummary, BillSummaryStatus

KIND = "bill_summary.stamp"


def title_for(summary: BillSummary) -> str:
    return f"Bill summary {summary.entry_no} (bill {summary.sap_invoice_doc_num})"


def link_for(summary: BillSummary) -> str:
    return f"/warehouse/bill-summaries/{summary.pk}"


class BillSummaryStampHandler:
    kind = KIND

    def send(self, posting):
        import requests

        from sap_mirror.services import hana_unreachable

        from .bill_summary_service import BillSummaryError, BillSummaryService

        summary = (
            BillSummary.objects.select_related("company").filter(pk=posting.source_id).first()
        )
        if summary is None:
            return Outcome.rejected("The bill summary no longer exists, so there is nothing to stamp.")
        clearing = summary.status == BillSummaryStatus.CANCELLED
        if not clearing and summary.dispatch_date is None:
            return Outcome.rejected(
                f"{summary.entry_no} has not been approved, so it has no dispatch date to put on the bill."
            )

        service = BillSummaryService(summary.company.code, user=posting.created_by)
        try:
            kept, dropped = service._patch_invoice(summary, clear=clearing)
        except (SAPConnectionError, requests.RequestException) as exc:
            return self._waiting(summary, exc)
        except SAPDataError as exc:
            # The read of what SAP already holds goes through HANA: HANA not
            # answering is a wait; SAP answering with a refusal is not.
            if hana_unreachable(exc):
                return self._waiting(summary, exc)
            BillSummaryService.record_refused(summary, str(exc))
            return Outcome.rejected(str(exc))
        except BillSummaryError as exc:
            BillSummaryService.record_refused(summary, str(exc))
            return Outcome.rejected(str(exc))

        BillSummaryService.record_posted(summary, clearing=clearing, kept=kept, dropped=dropped)
        what = "cleared from" if clearing else "stamped on"
        return Outcome.posted(
            f"Dispatch {what} invoice {summary.sap_invoice_doc_num}",
            result={"doc_nums": [summary.sap_invoice_doc_num], "cleared": clearing},
        )

    @staticmethod
    def _waiting(summary, exc):
        from .bill_summary_service import BillSummaryService

        message = f"SAP could not be reached ({exc}); the bill is not stamped yet"
        BillSummaryService.record_waiting(summary, message)
        return Outcome.waiting(message)
