"""What the posting log calls each unit, and where it links to.

Kept apart from ``sap_posting.py`` (which re-exports them, as the spec names
them there) so ``services.py`` can use them without importing the handlers.
"""


def link_for(bunch_id) -> str:
    return f"/accounts/bunches/{bunch_id}/sap"


def title_for_po(b) -> str:
    return f"Imprest PO · bunch {b.bunch.number} · {b.sap_company_code}"


def title_for_grpo(g) -> str:
    return f"Imprest GRPO · bunch {g.batch.bunch.number} · {g.num_at_card or g.group_key}"


def title_for_ap(d) -> str:
    return (
        f"Imprest A/P {d.kind.lower()} draft · bunch {d.batch.bunch.number} · "
        f"{d.sap_company_code}"
    )


def title_for_close(b) -> str:
    return f"Close imprest PO {b.po_doc_num} · bunch {b.bunch.number}"
