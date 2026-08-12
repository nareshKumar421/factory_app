"""Checking marketplace master data against the SAP item master.

Combo components, their alternatives and SKU mappings all name a SAP item code
and carry a copy of its name. Nothing checked either, so on 11 Aug 2026 combo
cb005 was saved with ``FG0000005`` labelled "EXTRA LIGHT OLIVE 3 LTR TIN 2 PCS"
— FG0000005 is actually EXTRA LIGHT OLIVE 1 LTR 16 PCS. Four confirmed
dispatches were queued to ship the wrong item, and nothing on the screen said so.

Two rules, applied wherever a code is saved:

* a code SAP has never heard of is rejected — it can only ship as a failure;
* a name that disagrees with SAP is overwritten with SAP's, so the stored code
  and name can never contradict each other. The code is the instruction; the
  name is a label for humans, and a label that lies is worse than none.

Both are best-effort: if HANA cannot be reached the save proceeds with a warning.
Master data must stay editable when SAP is down.
"""
import logging

from rest_framework import serializers

logger = logging.getLogger(__name__)


def lookup_names(company_code, item_codes):
    """``{code: ItemName}`` from SAP; ``None`` when the master cannot be read."""
    from .sap_gateway import oitm_names

    return oitm_names(company_code, item_codes)


def check_codes(company_code, item_codes):
    """(known, unknown) for a set of codes.

    ``unknown`` is empty when the master could not be READ — an unreachable SAP
    means "cannot say", and saying "does not exist" there would block every edit
    for the duration of an outage.

    That is why :func:`~marketplace.services.sap_gateway.oitm_names` returns
    ``None`` for unavailable rather than ``{}``: a payload whose codes are all
    unknown also produces ``{}``, and treating the two alike let exactly the
    codes this guards against save cleanly.
    """
    codes = [c for c in {(c or "").strip() for c in item_codes} if c]
    if not codes:
        return {}, []
    known = lookup_names(company_code, codes)
    if known is None:
        logger.warning(
            "SAP item master unreachable — %d code(s) saved unverified", len(codes))
        return {}, []
    return known, sorted(c for c in codes if c not in known)


def reject_unknown(company_code, item_codes, field="item_code"):
    """Raise if any code is absent from the SAP item master. Returns the names."""
    known, unknown = check_codes(company_code, item_codes)
    if unknown:
        raise serializers.ValidationError({
            field: (
                f"Not in the SAP item master: {', '.join(unknown)}. "
                f"Check the code — a wrong one ships the wrong product."
            )
        })
    return known


def apply_sap_name(entry, known, code_key="item_code", name_key="item_name"):
    """Overwrite the stored name with SAP's, so code and name cannot disagree.

    Mutates and returns ``entry``. A code the lookup could not confirm is left
    exactly as given.
    """
    code = (entry.get(code_key) or "").strip()
    sap_name = known.get(code)
    if code and sap_name:
        entry[name_key] = sap_name
    return entry
