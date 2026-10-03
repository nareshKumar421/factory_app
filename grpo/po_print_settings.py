"""Which approver a company's printed Purchase Order names.

SAP's own layout does not read the approver off the order: the name on it is
typed into the layout (see ``sap_client.hana.po_print_reader``). So the company
decides it here — either SAP's approval chain, as the reader returns it, or a
name entered on the Material GRPO page, printed as typed.

Applied once to the reader's payload by every view that serves the sheet, so the
gate's "Print PO" and the PM requirement board's order agree.
"""

from django.db import transaction

from .models import POApproverSource, POPrintSettings


def get_settings(company) -> POPrintSettings:
    """The company's settings, or an unsaved instance on the SAP default.

    Reading never writes, so ``pk is None`` means nobody has changed them.
    """
    found = POPrintSettings.objects.filter(company=company).first()
    return found or POPrintSettings(company=company)


@transaction.atomic
def update_settings(company, data: dict, user) -> POPrintSettings:
    """Save the source and the name, refusing a manual source with no name."""
    row, _ = POPrintSettings.objects.select_for_update().get_or_create(company=company)
    if "approver_source" in data:
        row.approver_source = data["approver_source"]
    if "approver_name" in data:
        row.approver_name = (data["approver_name"] or "").strip()

    if row.approver_source == POApproverSource.MANUAL and not row.approver_name:
        raise ValueError("Enter the approver's name to print it instead of SAP's.")

    row.updated_by = user
    row.save()
    return row


def apply_to_payload(payload: dict, company) -> dict:
    """Swap the SAP approver for the typed one when the company asks for it."""
    row = get_settings(company)
    if row.approver_source == POApproverSource.MANUAL and row.approver_name:
        payload.setdefault("approval", {})["approver"] = row.approver_name
    return payload
