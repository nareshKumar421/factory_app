"""
Single source of truth for the Jivo Wellness "Document Management Procedure"
(DOC-SOP-04-02-00-01) code tables.

Controlled-document code format::

    {SECTION}-{DOCTYPE}-{CC}-{SS}-{GG}[-{NN}]

Everything that varies by business policy lives in THIS FILE ONLY:

* ``SECTIONS``             -- department / section nomenclature codes.
* ``DOCTYPES``            -- document type codes.
* ``MODULE_DOCUMENT_TYPES`` -- which section / doctype / ISO 22000 clause a PDF
                             uploaded in a given application module is coded under.

New sections, document types or module mappings can be added here without any
other code change (Rule 1 of the procedure).
"""

# ---------------------------------------------------------------------------
# 1. SECTION -- department / section nomenclature codes.
# ---------------------------------------------------------------------------
SECTIONS = {
    "DOC": "Document",
    "PUR": "Purchase",
    "QA": "Quality",
    "PRD": "Production",
    "MIC": "Microbiology",
    "PST": "Pest Control",
    "CNS": "Cleaning & Sanitation",
    "STR": "Store",
    "HKG": "Housekeeping",
    "HR": "Human Resource",
    "UTL": "Utility",
    "WH": "Warehouse & Shipping",
    "FLW": "Flow Chart",
    "TACCP": "TACCP",
    "VACCP": "VACCP",
}

# ---------------------------------------------------------------------------
# 2. DOCTYPE -- document type codes.
# ---------------------------------------------------------------------------
DOCTYPES = {
    "SOP": "Standard Operating Procedure",
    "PGM": "Program",
    "FRM": "Form",
    "WI": "Work Instruction",
    "MAN": "Manual",
    "CHK": "Checklist",
    "FLW": "Flow Chart",
}

# ---------------------------------------------------------------------------
# 3/4. Module -> default controlled-document identity.
#
# When a PDF is uploaded / received in one of these application modules the
# shared numbering service issues the next code in this SECTION+DOCTYPE+clause
# group. ``clause`` is the CC-SS-GG triple (ISO 22000 clause / sub-clause;
# GG=00 means general).
# ---------------------------------------------------------------------------
MODULE_DOCUMENT_TYPES = {
    # Gate module -- vehicle gate entry / receiving documents.
    "GATE": {"section": "WH", "doctype": "FRM", "clause": "08-05-00"},
    # Quality Control -- inspection records & material arrival certificates.
    "QC": {"section": "QA", "doctype": "FRM", "clause": "08-06-00"},
    # Goods Receipt PO -- store receiving documents.
    "GRPO": {"section": "STR", "doctype": "FRM", "clause": "08-05-00"},
}


def get_module_document_type(module_key):
    """Return the ``{section, doctype, clause}`` mapping for a module key.

    Raises ``KeyError`` (with a helpful message) for an unmapped module so a
    typo surfaces loudly instead of minting a wrong code.
    """
    try:
        return MODULE_DOCUMENT_TYPES[module_key]
    except KeyError as exc:
        raise KeyError(
            f"No document-type mapping configured for module {module_key!r}. "
            f"Add it to MODULE_DOCUMENT_TYPES in document_control/config.py."
        ) from exc
