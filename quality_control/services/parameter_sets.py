# quality_control/services/parameter_sets.py
"""Resolving which QC parameter set applies to a given inspection.

A material type owns one default parameter set plus any number of
vendor-specific ones. Resolution is deliberately simple: the vendor's own set
if there is one, otherwise the default. Production QC has no vendor at all and
always lands on the default.
"""

from rest_framework.exceptions import ValidationError

from ..models import QCParameterSet


def normalize_vendor_code(vendor_code):
    return (vendor_code or "").strip().upper()


def active_parameter_sets(material_type):
    return material_type.parameter_sets.filter(is_active=True)


def default_parameter_set(material_type):
    """The material type's vendor-agnostic set, or None if never created."""
    return active_parameter_sets(material_type).filter(vendor_code="").first()


def vendor_parameter_set(material_type, vendor_code):
    """The set for one vendor, or None if that vendor uses the default."""
    normalized = normalize_vendor_code(vendor_code)
    if not normalized:
        return None
    return active_parameter_sets(material_type).filter(vendor_code=normalized).first()


def resolve_parameter_set(material_type, vendor_code=None, required=True):
    """Pick the parameter set to inspect against.

    Vendor-specific set wins; otherwise the default. When ``required`` and
    neither exists, raises a validation error telling the user to set the
    material type's parameters up first.
    """
    resolved = vendor_parameter_set(material_type, vendor_code)
    if resolved is not None:
        return resolved

    resolved = default_parameter_set(material_type)
    if resolved is not None:
        return resolved

    if required:
        raise ValidationError({
            "parameter_set": [
                f'Material type "{material_type.code}" has no QC parameters yet. '
                f"Add its default parameter set before inspecting against it."
            ]
        })
    return None


def get_or_create_default_set(material_type, user=None):
    """Ensure the material type has a default set, creating an empty one if not.

    Used when parameters are added through the older material-type-scoped API,
    which has no notion of sets.
    """
    existing = active_parameter_sets(material_type).filter(vendor_code="").first()
    if existing:
        return existing

    # A soft-deleted default is revived rather than duplicated: the unique
    # constraint covers inactive rows too.
    archived = material_type.parameter_sets.filter(vendor_code="").first()
    if archived:
        archived.is_active = True
        archived.updated_by = user
        archived.save(update_fields=["is_active", "updated_by", "updated_at"])
        return archived

    return QCParameterSet.objects.create(
        material_type=material_type,
        vendor_code="",
        vendor_name="",
        created_by=user,
        updated_by=user,
    )


def copy_parameters(source_set, target_set, user=None):
    """Copy every active parameter from one set onto another.

    Rows already on the target with the same ``parameter_code`` are updated in
    place (and revived if they had been deleted), so copying twice is safe and
    keeps existing results pointing at the same parameter row.
    """
    from ..models import QCParameterMaster

    source_parameters = list(
        source_set.parameters.filter(is_active=True).order_by("sequence", "id")
    )
    if not source_parameters:
        raise ValidationError({
            "source_parameter_set_id": [
                "The set you are copying from has no active QC parameters."
            ]
        })

    source_codes = [param.parameter_code for param in source_parameters]
    existing_parameters = {
        param.parameter_code: param
        for param in target_set.parameters.filter(parameter_code__in=source_codes)
    }

    copy_fields = [
        "parameter_name",
        "parameter_code",
        "standard_value",
        "parameter_type",
        "min_value",
        "max_value",
        "uom",
        "sequence",
        "is_mandatory",
    ]

    copied_count = 0
    updated_count = 0
    copied_parameters = []

    for source_param in source_parameters:
        parameter_data = {field: getattr(source_param, field) for field in copy_fields}
        target_param = existing_parameters.get(source_param.parameter_code)

        if target_param:
            for field, value in parameter_data.items():
                setattr(target_param, field, value)
            target_param.is_active = True
            target_param.updated_by = user
            target_param.save()
            updated_count += 1
        else:
            target_param = QCParameterMaster.objects.create(
                parameter_set=target_set,
                created_by=user,
                updated_by=user,
                **parameter_data
            )
            copied_count += 1

        copied_parameters.append(target_param)

    return copied_count, updated_count, copied_parameters


def sync_result_rows(container, parameter_set, user, result_model, container_field):
    """Bring a container's result rows in line with a parameter set.

    Rows for parameters that are no longer in the set are deactivated, rows for
    new parameters are created with the definition snapshotted onto them, and
    rows that survive get their snapshot refreshed — but only while the reading
    is still editable, which the caller enforces.
    """
    active_params = list(parameter_set.parameters.filter(is_active=True))
    active_param_ids = [param.id for param in active_params]

    existing_rows = result_model.objects.filter(**{container_field: container})
    existing_rows.exclude(parameter_master_id__in=active_param_ids).update(
        is_active=False, updated_by=user
    )

    existing_by_param = {
        row.parameter_master_id: row
        for row in existing_rows.filter(parameter_master_id__in=active_param_ids)
    }

    for param in active_params:
        row = existing_by_param.get(param.id)
        if row is None:
            new_row = result_model(**{container_field: container})
            new_row.parameter_master = param
            new_row.apply_parameter_snapshot(param)
            new_row.created_by = user
            new_row.updated_by = user
            new_row.save()
            continue

        row.is_active = True
        row.apply_parameter_snapshot(param)
        row.updated_by = user
        row.save()


def ensure_vendor_code_is_free(material_type, vendor_code, exclude_id=None):
    """Reject a vendor that already has a set on this material type.

    ``vendor_code`` is unique per material type including soft-deleted rows, so
    without this the clash would surface as an IntegrityError instead of a field
    error the form can show.
    """
    normalized = normalize_vendor_code(vendor_code)
    clashes = material_type.parameter_sets.filter(vendor_code=normalized)
    if exclude_id is not None:
        clashes = clashes.exclude(id=exclude_id)

    clash = clashes.first()
    if clash is None:
        return None

    if clash.is_active:
        raise ValidationError({
            "vendor_code": [
                f'"{clash.label}" already has a parameter set for this material type.'
            ]
        })

    # A deleted set is handed back so the caller can revive it instead of
    # blocking the user on a row they can no longer see.
    return clash


__all__ = [
    "normalize_vendor_code",
    "active_parameter_sets",
    "default_parameter_set",
    "vendor_parameter_set",
    "resolve_parameter_set",
    "get_or_create_default_set",
    "copy_parameters",
    "sync_result_rows",
    "ensure_vendor_code_is_free",
]
