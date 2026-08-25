"""
Change-log plumbing for the ETP / STP registers.

One job: turn "this row was saved" into a readable line of the edit trail —
which fields moved, from what to what, and who moved them. The viewsets take a
snapshot before the write, another after, and hand both to
:func:`record_change`.

Values are snapshotted as display strings (a foreign key becomes the related
object's name) so the trail stays readable years later, when the row it points
at may have been deleted and the master it referenced renamed.
"""

from datetime import date as date_cls
from datetime import datetime, time
from decimal import Decimal

from django.db import models

from .constants import ChangeAction
from .models import RegisterChangeLog

#: Bookkeeping columns: they are the audit, not the data being audited.
SKIP_FIELDS = {"id", "created_at", "updated_at", "created_by", "updated_by"}

#: Field names read as a plain word in a summary line, without the model's
#: underscores.
FIELD_LABELS = {
    "inlet_initial": "inlet initial",
    "inlet_final": "inlet final",
    "outlet_initial": "outlet initial",
    "outlet_final": "outlet final",
    "energy_initial": "energy initial",
    "energy_final": "energy final",
    "ph_reading": "pH reading",
    "quantity_kg": "quantity (kg)",
    "contact_minutes": "contact minutes",
    "is_out_of_calibration": "out of calibration",
    "corrective_action": "corrective action",
    "collection_mode": "mode of collection",
    "storage_method": "method of storage",
    "disposal_mode": "mode of disposal",
    "verified_by": "verified by",
    "checked_by": "checked by",
    "interval_hours": "sampling interval (h)",
}


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name, name.replace("_", " "))


def _display(value):
    """A JSON-safe, human-readable rendering of one field value."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date_cls, time)):
        return value.isoformat(timespec="minutes") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def snapshot(instance, extra=None) -> dict:
    """The instance's auditable fields as display values.

    ``extra`` carries anything that is not a column on the row itself — the
    child rows of the registers that own a grid (monitoring readings, chemical
    lines, calibration readings), so replacing them shows up as a change too.
    """
    data = {}
    for field in instance._meta.concrete_fields:
        if field.name in SKIP_FIELDS:
            continue
        value = getattr(instance, field.name, None)
        if isinstance(field, models.ForeignKey):
            data[field.name] = str(value) if value is not None else None
        else:
            data[field.name] = _display(value)
    if extra:
        data.update({key: _display(value) for key, value in extra.items()})
    return data


def diff(before: dict, after: dict) -> dict:
    """``{field: {"from": old, "to": new}}`` for the fields that actually moved."""
    changed = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        # "" and None both mean "nothing recorded"; a save that flips between the
        # two is not a change anyone needs to read about.
        if (old in (None, "")) and (new in (None, "")):
            continue
        changed[key] = {"from": old, "to": new}
    return changed


def _summarise(action: str, changes: dict, limit: int = 4) -> str:
    if action == ChangeAction.CREATED:
        return "Recorded"
    if action == ChangeAction.DELETED:
        return "Deleted"
    if action == ChangeAction.VERIFIED:
        return "Verified"
    if not changes:
        return "Saved with no change"
    parts = [
        f"{field_label(field)} {values['from'] if values['from'] not in (None, '') else '—'}"
        f" → {values['to'] if values['to'] not in (None, '') else '—'}"
        for field, values in list(changes.items())[:limit]
    ]
    if len(changes) > limit:
        parts.append(f"and {len(changes) - limit} more")
    return "; ".join(parts)


def record_change(
    *,
    register: str,
    action: str,
    instance,
    before: dict | None = None,
    after: dict | None = None,
    user=None,
    plant=None,
    entry_date=None,
    object_id: int | None = None,
) -> RegisterChangeLog | None:
    """Append one line to the trail.

    An update that moved nothing is not logged — an operator opening a day and
    pressing Save should not bury the edit that mattered.
    """
    changes = diff(before or {}, after or {}) if action == ChangeAction.UPDATED else {}
    if action == ChangeAction.UPDATED and not changes:
        return None
    return RegisterChangeLog.objects.create(
        register=register,
        action=action,
        # Passed explicitly on a delete: the instance's pk is gone by then.
        object_id=object_id if object_id is not None else getattr(instance, "pk", None),
        model_name=instance._meta.model_name if instance is not None else "",
        plant=plant,
        entry_date=entry_date,
        changes=changes,
        summary=_summarise(action, changes),
        changed_by=user if (user is not None and user.is_authenticated) else None,
    )


# ---------------------------------------------------------------------------
# Child-row snapshots
#
# Three registers own a grid whose rows are replaced wholesale on save. Their
# children are folded into one readable string each, so "the 14:00 reading was
# corrected" shows up in the trail instead of nothing.
# ---------------------------------------------------------------------------


def describe_monitoring_readings(record) -> str:
    rows = []
    for reading in record.readings.all().prefetch_related("values__parameter"):
        cells = ", ".join(
            f"{value.parameter.parameter_name} {value.value}"
            for value in reading.values.all()
        )
        rows.append(f"{reading.reading_time:%H:%M} {cells}".strip())
    return " | ".join(rows)


def describe_chemical_lines(log) -> str:
    return "; ".join(
        f"{line.chemical.name} {line.quantity} {line.uom}"
        for line in log.lines.all().select_related("chemical")
    )


def describe_calibration_readings(record) -> str:
    return "; ".join(
        f"{reading.actual_value}→{reading.observed_value}"
        for reading in record.readings.all()
    )
