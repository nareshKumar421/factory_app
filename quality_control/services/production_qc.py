# quality_control/services/production_qc.py
"""QA Reports: saving an entry of a report, and approving or sending it back."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..enums import ParameterType
from ..models import (
    ProductionParameterType,
    ProductionParameterTypeDefault,
    ProductionQCEntry,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionQCSubmission,
)

class ProductionQCError(Exception):
    """An entry that cannot be saved or decided; the message is for the user."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field

    def as_response_data(self):
        return {self.field: [str(self)]} if self.field else {"detail": str(self)}


def _parse_numeric(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _apply_reading(row, value_type, data, user):
    """Put one reading on a result row; save() then judges it against the row's spec."""
    value = (data.get("result_value") or "").strip()
    numeric = data.get("result_numeric")
    if numeric is None and value and value_type in (ParameterType.NUMERIC, ParameterType.RANGE):
        numeric = _parse_numeric(value)
    within_spec = data.get("is_within_spec")
    if value_type == ParameterType.BOOLEAN and value:
        # A pass/fail reading is its own verdict, as on the arrival slip.
        within_spec = value.lower() == "pass"
    row.result_value = value
    row.result_numeric = numeric
    row.is_within_spec = within_spec
    row.remarks = (data.get("remarks") or "").strip()
    row.updated_by = user
    row.save()


def _check_readings(definitions, readings):
    """`definitions`: (parameter_id, name, is_mandatory) for each parameter asked for."""
    unknown = set(readings) - {parameter_id for parameter_id, _, _ in definitions}
    if unknown:
        raise ProductionQCError("A reading was sent for a parameter not in this type.", "results")
    missing = [
        name
        for parameter_id, name, mandatory in definitions
        if mandatory and not (readings.get(parameter_id, {}).get("result_value") or "").strip()
    ]
    if missing:
        raise ProductionQCError(
            f"Enter a value for every mandatory parameter: {', '.join(missing)}.", "results"
        )


def _require_remark_if_out_of_spec(entries):
    """Entries sent together share one remark: it is needed if any of them is out of spec."""
    for entry in entries:
        if entry.results.filter(is_within_spec=False).exists() and not entry.remarks.strip():
            raise ProductionQCError(
                "A remark is required when any parameter is out of spec.", "remarks"
            )


def _check_samples(definitions, samples):
    """Check each sample's readings; with several, say which sample is short."""
    for index, readings in enumerate(samples, 1):
        try:
            _check_readings(definitions, readings)
        except ProductionQCError as exc:
            if len(samples) == 1:
                raise
            raise ProductionQCError(f"Sample {index}: {exc}", exc.field) from None


def _siblings(entry):
    """The entry and the others sent with it, locked: they are decided as one."""
    if entry.submission_id:
        return list(
            ProductionQCEntry.objects.select_for_update()
            .filter(submission_id=entry.submission_id, is_active=True)
            .order_by("id")
        )
    return [ProductionQCEntry.objects.select_for_update().get(pk=entry.pk)]


def _parameters_of(parameter_type):
    parameters = list(parameter_type.parameters.filter(is_active=True).order_by("sequence", "id"))
    if not parameters:
        raise ProductionQCError(
            "This report has no parameters yet. Add them under Report Types first.",
            "parameter_type_id",
        )
    return parameters


def _default_of(parameter_type, default_id):
    """The report's active default the entry is made with, or None for its own standards."""
    if not default_id:
        return None
    default = (
        ProductionParameterTypeDefault.objects.filter(
            parameter_type=parameter_type, pk=default_id, is_active=True
        )
        .prefetch_related("values")
        .first()
    )
    if default is None:
        raise ProductionQCError(
            "That default is not one of this report's any more. Pick it again.", "default_id"
        )
    return default


def _apply_default_spec(row, value):
    """A default that sets a spec replaces the parameter's standard, min and max together."""
    if value is None or not value.sets_spec:
        return
    row.standard_value = value.standard_value.strip() or "-"
    row.min_value = value.min_value
    row.max_value = value.max_value


@transaction.atomic
def create_entries(company, user, *, parameter_type_id, samples, remarks="", default_id=None):
    """Save one or more entries of a report, filled together, and send them for approval as one.

    Each sample — a mould, a bottle — is its own entry: its own row in the list,
    its own column on the sheet. They share the default, the time, the remark
    and, later, the decision. A report is not tied to production: there is no
    line or run to pick. With one of the report's defaults, each entry is judged
    against that default's standards wherever it sets them (its pre-filled values
    arrive as readings).
    """
    if not samples:
        raise ProductionQCError("Fill at least one sample.", "samples")
    parameter_type = ProductionParameterType.objects.filter(
        company=company, pk=parameter_type_id, is_active=True
    ).first()
    if parameter_type is None:
        raise ProductionQCError("Pick a report.", "parameter_type_id")
    default = _default_of(parameter_type, default_id)
    default_values = {v.parameter_id: v for v in default.values.all()} if default else {}

    parameters = _parameters_of(parameter_type)
    _check_samples([(p.id, p.parameter_name, p.is_mandatory) for p in parameters], samples)

    now = timezone.now()
    submission = ProductionQCSubmission.objects.create(company=company, created_by=user)
    entries = []
    for readings in samples:
        entry = ProductionQCEntry.objects.create(
            company=company,
            parameter_type=parameter_type,
            default=default,
            default_name=default.name if default else "",
            submission=submission,
            checked_at=now,
            status=ProductionQCStatus.PENDING,
            remarks=(remarks or "").strip(),
            submitted_by=user,
            submitted_at=now,
            created_by=user,
            updated_by=user,
        )
        for parameter in parameters:
            row = ProductionQCResult(entry=entry, parameter_master=parameter, created_by=user)
            row.apply_parameter_snapshot(parameter)
            _apply_default_spec(row, default_values.get(parameter.id))
            _apply_reading(row, parameter.value_type, readings.get(parameter.id, {}), user)
        entries.append(entry)
    _require_remark_if_out_of_spec(entries)
    return entries


def create_entry(company, user, *, parameter_type_id, readings, remarks="", default_id=None):
    """One entry on its own: a submission of one."""
    return create_entries(
        company, user, parameter_type_id=parameter_type_id, samples=[readings],
        remarks=remarks, default_id=default_id,
    )[0]


@transaction.atomic
def update_entries(entry, user, *, readings_by_entry, remarks=""):
    """Correct an entry and the others sent with it, and send them all again.

    They were sent and are decided as one, so they are corrected as one: every
    entry of the submission, keyed by its id.
    """
    entries = _siblings(entry)
    if any(e.status == ProductionQCStatus.APPROVED for e in entries):
        raise ProductionQCError("An approved entry cannot be changed.")
    if set(readings_by_entry) != {e.pk for e in entries}:
        raise ProductionQCError(
            "Correct it with the entries sent with it: send every one of them.", "samples"
        )

    # A correction is judged against the spec the entry was made against: the
    # rows keep their snapshot, and only the readings on them change.
    rows_by_entry = {e.pk: list(e.results.order_by("sequence", "id")) for e in entries}
    for index, e in enumerate(entries, 1):
        definitions = [
            (r.parameter_master_id, r.parameter_name, r.is_mandatory) for r in rows_by_entry[e.pk]
        ]
        try:
            _check_readings(definitions, readings_by_entry[e.pk])
        except ProductionQCError as exc:
            if len(entries) == 1:
                raise
            raise ProductionQCError(f"Sample {index}: {exc}", exc.field) from None

    now = timezone.now()
    for e in entries:
        e.remarks = (remarks or "").strip()
        e.status = ProductionQCStatus.PENDING
        e.submitted_by = user
        e.submitted_at = now
        e.updated_by = user
        e.save()
        readings = readings_by_entry[e.pk]
        for row in rows_by_entry[e.pk]:
            _apply_reading(row, row.parameter_type, readings.get(row.parameter_master_id, {}), user)
    _require_remark_if_out_of_spec(entries)
    return entries


def update_entry(entry, user, *, readings, remarks=""):
    """Correct an entry sent on its own; one sent with others is corrected with them."""
    entries = update_entries(entry, user, readings_by_entry={entry.pk: readings}, remarks=remarks)
    return next(e for e in entries if e.pk == entry.pk)


@transaction.atomic
def approve_entry(entry, user, remarks=""):
    """Approve the entry and every entry sent with it: they are decided as one."""
    entries = _siblings(entry)
    if any(e.status != ProductionQCStatus.PENDING for e in entries):
        raise ProductionQCError("Only an entry waiting for approval can be approved.")
    now = timezone.now()
    for e in entries:
        e.status = ProductionQCStatus.APPROVED
        e.approved_by = user
        e.approved_at = now
        e.approval_remarks = (remarks or "").strip()
        e.updated_by = user
        e.save()
    return next(e for e in entries if e.pk == entry.pk)


@transaction.atomic
def send_back_entry(entry, user, remarks):
    remarks = (remarks or "").strip()
    if not remarks:
        raise ProductionQCError("Say what needs correcting.", "remarks")
    entries = _siblings(entry)
    if any(e.status != ProductionQCStatus.PENDING for e in entries):
        raise ProductionQCError("Only an entry waiting for approval can be sent back.")
    now = timezone.now()
    for e in entries:
        e.status = ProductionQCStatus.SENT_BACK
        e.sent_back_by = user
        e.sent_back_at = now
        e.send_back_remarks = remarks
        e.updated_by = user
        e.save()
    return next(e for e in entries if e.pk == entry.pk)
