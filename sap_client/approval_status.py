"""When is a SAP approval request really pending?

Ported from SAP Portal (``backend_v1/services/sapApprovals.js``,
``effectiveOwddStatus`` / ``effectiveStatusSql``). SAP often leaves an
approval request header (``OWDD``) at ``Status = 'W'`` after the approval of
its draft was cancelled, rejected or already posted — editing a draft, for
one, cancels its request and opens a new one without closing the old header.
The draft's own ``WddStatus`` (``ODRF`` for documents, ``OPDF`` for payment
drafts) is what SAP actually acts on, so a request counts as pending only while
its draft says ``'W'`` too. Anything else is a leftover.

JI's three existing approval readers (A/R invoice, transfer and credit-note
drafts) each spell the same rule inline in their own SQL. New readers use this
module; the Python and SQL halves must stay in step, which
``sap_client.tests_sap_portal`` pins with the portal's own cases.
"""

# Draft outcomes that are more specific than "cancelled". Any other draft state
# (missing draft, no longer under approval) reads as cancelled.
DRAFT_OUTCOMES = ("N", "Y", "P", "A")

# OWDD.Status codes → Service Layer ApprovalRequest.Status values.
OWDD_TO_SL = {
    "W": "arsPending",
    "Y": "arsApproved",
    "N": "arsNotApproved",
    "P": "arsGenerated",
    "A": "arsGeneratedByAuthorizer",
    "C": "arsCancelled",
}

# OWDD.Status codes → the app's status words.
OWDD_TO_APP = {
    "W": "PENDING",
    "Y": "APPROVED",
    "N": "REJECTED",
    "P": "GENERATED",
    "A": "GENERATED",
    "C": "CANCELLED",
}

# The app's status filter → the OWDD codes it covers (the portal's tabs).
APP_FILTER_TO_OWDD = {
    "PENDING": ("W",),
    "APPROVED": ("Y", "P", "A"),
    "REJECTED": ("N",),
    "GENERATED": ("P", "A"),
    "CANCELLED": ("C",),
}

# Document object types that live in ODRF-backed drafts versus payment drafts.
PAYMENT_OBJECT_TYPES = ("24", "46")

# What the draft's status expression reads, given the joins in draft_join_sql().
DRAFT_STATUS_SQL = 'COALESCE(DR."WddStatus", PD."WddStatus")'


def is_draft_flag(value) -> bool:
    """``OWDD.IsDraft`` as HANA (``'Y'``) or the Service Layer (``'tYES'``) spells it."""
    return value in ("Y", "tYES")


def effective_status(owdd_status: str, is_draft, draft_status) -> str:
    """The OWDD code SAP would act on for this request.

    >>> effective_status("W", "Y", "W")
    'W'
    >>> effective_status("W", "Y", "C")
    'C'
    >>> effective_status("W", "Y", "N")
    'N'
    >>> effective_status("Y", "Y", "W")
    'Y'
    """
    if owdd_status != "W" or not is_draft_flag(is_draft):
        return owdd_status
    if draft_status == "W":
        return "W"
    return draft_status if draft_status in DRAFT_OUTCOMES else "C"


def effective_status_sql(alias: str, draft_status_sql: str = DRAFT_STATUS_SQL) -> str:
    """:func:`effective_status` as a HANA expression over OWDD alias ``alias``."""
    outcomes = ", ".join(f"'{code}'" for code in DRAFT_OUTCOMES)
    return (
        f"""(CASE WHEN {alias}."Status" = 'W' AND {alias}."IsDraft" = 'Y' """
        f"""AND COALESCE({draft_status_sql}, '') <> 'W' """
        f"""THEN (CASE WHEN {draft_status_sql} IN ({outcomes}) THEN {draft_status_sql} ELSE 'C' END) """
        f"""ELSE {alias}."Status" END)"""
    )


def draft_join_sql(alias: str = "W", schema_token: str = "{schema}") -> str:
    """LEFT JOINs to a request's draft: ``DR`` (ODRF) or ``PD`` (OPDF, payments)."""
    return (
        f'LEFT JOIN "{schema_token}"."ODRF" DR '
        f'ON DR."DocEntry" = {alias}."DraftEntry" AND DR."ObjType" = {alias}."ObjType" '
        f'LEFT JOIN "{schema_token}"."OPDF" PD '
        f'ON PD."DocEntry" = {alias}."DraftEntry" AND PD."ObjType" = {alias}."ObjType"'
    )


# A draft's AuthorizationStatus as the Service Layer spells it (``dasPending``,
# ``pasCancelled`` on payment drafts, …) → the ODRF/OPDF WddStatus code.
_SL_AUTHORIZATION_TO_WDD = {
    "pending": "W",
    "without": "-",
    "approved": "Y",
    "rejected": "N",
    "generated": "P",
    "generatedbyauthorizer": "A",
    "cancelled": "C",
}


def draft_status_from_sl(value) -> str | None:
    """``dasCancelled`` → ``'C'``; None for anything unrecognised."""
    key = str(value or "")
    for prefix in ("das", "pas"):
        if key.lower().startswith(prefix):
            key = key[len(prefix):]
            break
    return _SL_AUTHORIZATION_TO_WDD.get(key.lower())


def owdd_codes_to_scan(filter_codes) -> tuple:
    """Every leftover is still a 'W' row, so any status filter must scan 'W' too."""
    codes = tuple(filter_codes)
    return codes if "W" in codes else codes + ("W",)
