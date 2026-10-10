"""Has the bunch changed in the cash book since it was frozen for SAP?

The spec locked the bunch in ``cash_book`` once posting began
(``CashBunch.sap_locked_at``). This branch may not edit ``cash_book``, so the
bunch cannot be locked; instead its members are snapshotted at the first
freeze (``BunchSapState.member_snapshot``) and compared before every freeze,
post, send-next and handler send. A difference -- a voucher taken out, one that
appeared, an amount, G/L, branch or date that moved -- stops everything with
``BUNCH_CHANGED``. Nothing more goes to SAP until a person resets the bunch or
accepts the change.

Pure apart from :func:`current_members`, which reads the bunch's active
entries once.
"""

from typing import Dict

from .constants import BUNCH_CHANGED


class BunchChangedError(Exception):
    """Raised internally; services turn it into a DRF 400 with ``code=BUNCH_CHANGED``."""

    code = BUNCH_CHANGED

    def __init__(self, drift: dict, bunch_number=None):
        self.drift = drift
        self.bunch_number = bunch_number
        super().__init__(describe(drift, bunch_number))


def member_row(entry) -> dict:
    return {
        "amount": f"{entry.amount:.2f}",
        "gl": entry.gl_account_code or "",
        "branch": entry.branch_id,
        "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
    }


def current_members(bunch) -> Dict[str, dict]:
    """``{"<entry id>": member_row}`` for the bunch's active entries now."""
    entries = bunch.entries.filter(is_active=True).only(
        "id", "amount", "gl_account_code", "branch", "entry_date"
    )
    return {str(e.id): member_row(e) for e in entries}


def compare(snapshot: dict, current: dict) -> dict:
    """``{}`` when equal, else ``{"added": [...], "removed": [...], "changed": {...}}``.

    Ids are ints in the result, sorted; ``changed`` maps an id to
    ``{field: [then, now]}``.
    """
    snapshot = snapshot or {}
    added = sorted(int(k) for k in current if k not in snapshot)
    removed = sorted(int(k) for k in snapshot if k not in current)
    changed = {}
    for key in sorted(set(snapshot) & set(current), key=int):
        then, now = snapshot[key], current[key]
        fields = {
            f: [then.get(f), now.get(f)]
            for f in ("amount", "gl", "branch", "entry_date")
            if then.get(f) != now.get(f)
        }
        if fields:
            changed[str(key)] = fields
    if not (added or removed or changed):
        return {}
    return {"added": added, "removed": removed, "changed": changed}


def describe(drift: dict, bunch_number=None) -> str:
    """One sentence for the screen and the posting log."""
    if not drift:
        return ""
    parts = []
    if drift.get("removed"):
        parts.append("taken out: " + ", ".join(str(i) for i in drift["removed"]))
    if drift.get("added"):
        parts.append("added: " + ", ".join(str(i) for i in drift["added"]))
    if drift.get("changed"):
        parts.append("changed: " + ", ".join(str(i) for i in drift["changed"]))
    which = f"Bunch {bunch_number}" if bunch_number is not None else "This bunch"
    return (
        f"{which} changed in the cash book after it was frozen for SAP "
        f"(vouchers {'; '.join(parts)}). Nothing more is sent until it is reset "
        f"or the change is accepted on the bunch's SAP page."
    )


def state_drift(bunch) -> dict:
    """The drift of a frozen bunch, ``{}`` when it is not frozen or unchanged."""
    from .models import BunchSapState

    state = BunchSapState.objects.filter(bunch_id=bunch.pk).first()
    if state is None or not state.is_frozen:
        return {}
    return compare(state.member_snapshot, current_members(bunch))
