"""The approval ladder: who may act on a request at each status, and where it goes next.

SAP Portal's rules (``backend_v1/server.js`` lines 24-54), with the role names
turned into rights (see ``permissions``):

==========  ===================  ===================  ==================  ==================
levels      PENDING              L1_APPROVED          L2_APPROVED         L3_APPROVED
==========  ===================  ===================  ==================  ==================
2           level 1              **push** (final)     --                  --
3           level 1              level 2              **push** (final)    --
4           level 1              level 2              push                **push** (final)
==========  ===================  ===================  ==================  ==================

With two levels the level-2 right is unused: the push follows level 1 directly.
With four, the push right signs twice, and the same-person rule (one approval
per person per request, server.js lines 454-458) makes those two different
people -- the portal's "SAP Adder L1" and "SAP Adder L2".

A request always sits at the step whose index is the number of approvals it
has (``OPEN_STATUSES``). If the setting is lowered while requests are in
flight, a request already past the new final step is treated as at the final
step, so it can still be pushed instead of being stranded.

Whoever may approve at a status may reject there (the portal checked one rule
for both actions, server.js lines 452-458) -- including the same-person rule,
so someone who approved an earlier level cannot reject a later one either.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .constants import OPEN_STATUSES, BOMChangeStatus
from .permissions import LEVEL_1_PERMISSION, LEVEL_2_PERMISSION, PUSH_PERMISSION

ALLOWED_LEVELS = (2, 3, 4)


def approval_levels() -> int:
    levels = getattr(settings, "BOM_CHANGE_APPROVAL_LEVELS", 3)
    if levels not in ALLOWED_LEVELS:
        raise ImproperlyConfigured(
            f"BOM_CHANGE_APPROVAL_LEVELS must be 2, 3 or 4, not {levels!r}."
        )
    return levels


def step_of(status) -> int | None:
    """Approvals a request at ``status`` already has; ``None`` once it is closed."""
    try:
        return OPEN_STATUSES.index(status)
    except ValueError:
        return None


def is_final(status, levels: int | None = None) -> bool:
    """Approving at ``status`` is the last sign-off: it writes SAP."""
    levels = levels or approval_levels()
    step = step_of(status)
    return step is not None and step >= levels - 1


def right_for(status, levels: int | None = None) -> str | None:
    """The right that approves (or rejects) a request at ``status``."""
    levels = levels or approval_levels()
    step = step_of(status)
    if step is None:
        return None
    if step >= levels - 1:
        return PUSH_PERMISSION
    if step == 0:
        return LEVEL_1_PERMISSION
    if step == 1:
        return LEVEL_2_PERMISSION
    # Step 2 is not final only with four levels: the first of two pushers.
    return PUSH_PERMISSION


def level_of(status, levels: int | None = None) -> int | None:
    """The sign-off an approval at ``status`` is (1-based), capped at the final one."""
    levels = levels or approval_levels()
    step = step_of(status)
    if step is None:
        return None
    return min(step, levels - 1) + 1


def next_status(status, levels: int | None = None) -> str | None:
    """Where an approval at ``status`` takes the request."""
    levels = levels or approval_levels()
    step = step_of(status)
    if step is None:
        return None
    if is_final(status, levels):
        return BOMChangeStatus.SAP_PUSHED
    return OPEN_STATUSES[step + 1]


_RIGHT_LABELS = {
    LEVEL_1_PERMISSION: "Level 1 approval",
    LEVEL_2_PERMISSION: "Level 2 approval",
    PUSH_PERMISSION: "SAP push",
}


def awaiting_label(status, levels: int | None = None) -> str:
    """What a request at ``status`` is waiting for, as the list shows it."""
    levels = levels or approval_levels()
    right = right_for(status, levels)
    if right is None:
        return ""
    if right == PUSH_PERMISSION and not is_final(status, levels):
        return "First SAP push approval"
    if right == PUSH_PERMISSION and levels == 4:
        return "Second SAP push approval (writes SAP)"
    if right == PUSH_PERMISSION:
        return "Final approval (writes SAP)"
    return _RIGHT_LABELS[right]


def steps(levels: int | None = None) -> list[dict]:
    """The ladder for the configured number of levels, for the page's pipeline."""
    levels = levels or approval_levels()
    out = []
    for status in OPEN_STATUSES[:levels]:
        out.append(
            {
                "level": level_of(status, levels),
                "status": status,
                "right": right_for(status, levels),
                "label": awaiting_label(status, levels),
                "writes_sap": is_final(status, levels),
            }
        )
    return out


def progress(status, approvals, levels: int | None = None) -> list[dict]:
    """``steps()`` marked for one request: done, current, rejected, skipped or upcoming.

    ``approvals`` are the request's decision rows. A direct push (level 0)
    skipped every level, so its levels read "skipped" rather than "done".
    """
    levels = levels or approval_levels()
    decided = {}
    direct = False
    for approval in approvals:
        if approval.level == 0:
            direct = True
            continue
        decided[approval.level] = approval.action
    current = level_of(status, levels)
    out = []
    for step in steps(levels):
        level = step["level"]
        if decided.get(level) == "REJECT":
            state = "rejected"
        elif decided.get(level) == "APPROVE":
            state = "done"
        elif status == BOMChangeStatus.SAP_PUSHED:
            state = "skipped" if direct else "done"
        elif current is not None and level == current:
            state = "current"
        else:
            state = "upcoming"
        out.append({**step, "state": state})
    return out
