"""
Reading and writing the ownership chart.

Every call is scoped to one company: Oil, Mart and Beverages each keep their
own chart, and a save must never reach across into another company's blocks.

The page saves the chart WHOLE, not row by row. Editing it is a sit-down job —
somebody renames a section, moves a row, adds two names and drops one — and a
single atomic save is both what the screen means by "Save" and the only way to
let names swap without a half-applied chart in between. The write is a diff, so
rows that were only moved keep their ids (and their created/updated stamps)
instead of being recreated.
"""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import OrgChartSettings, OrgDepartment, OrgFunction

#: Raised as-is when the chart changed underneath the editor.
STALE_MESSAGE = (
    "The chart changed since this page was opened. Reload it and re-apply your edits."
)


def get_settings(company):
    """This company's heading — plant name and plant head."""
    return OrgChartSettings.load(company)


def get_chart(company):
    """This company's departments with their functions, in chart order."""
    return list(
        OrgDepartment.objects.filter(company=company)
        .prefetch_related("functions")
        .order_by("sort_order", "name")
    )


def _stamp(instance, user):
    if user is not None and getattr(user, "is_authenticated", False):
        instance.updated_by = user
        if instance.pk is None:
            instance.created_by = user


@transaction.atomic
def save_chart(validated, company, user=None):
    """Replace ``company``'s chart with ``validated``; return the saved chart.

    ``validated`` is the output of
    :class:`~org_chart.serializers.ChartSaveSerializer`. Rows carrying an ``id``
    are updated in place, rows without one are created, and anything the payload
    no longer mentions is deleted — the editor always sends the complete chart.

    The heading is updated only for the keys the payload actually carries, so a
    client that does not know about ``plant_name`` cannot blank it.
    """
    settings = OrgChartSettings.load(company)
    heading_changed = False
    for field in ("plant_name", "plant_head"):
        if field in validated:
            setattr(settings, field, validated[field])
            heading_changed = True
    if heading_changed:
        _stamp(settings, user)
        settings.save()

    departments_data = validated["departments"]
    # Only this company's rows are in play: an id belonging to another company's
    # chart must read as "gone" (a stale editor), never as a row to overwrite.
    existing_departments = {
        d.pk: d for d in OrgDepartment.objects.filter(company=company)
    }
    existing_functions = {
        f.pk: f for f in OrgFunction.objects.filter(department__company=company)
    }

    kept_department_ids, kept_function_ids = set(), set()

    for order, data in enumerate(departments_data):
        department_id = data.get("id")
        if department_id:
            department = existing_departments.get(department_id)
            if department is None:
                raise ValidationError(STALE_MESSAGE)
        else:
            department = OrgDepartment()
        department.company = company
        department.name = data["name"]
        if "head" in data:
            department.head = data["head"]
        department.sort_order = order
        _stamp(department, user)
        department.save()
        kept_department_ids.add(department.pk)

        for function_order, function_data in enumerate(data.get("functions", [])):
            function_id = function_data.get("id")
            if function_id:
                function = existing_functions.get(function_id)
                if function is None:
                    raise ValidationError(STALE_MESSAGE)
            else:
                function = OrgFunction()
            # Assigned every time: a row may have been dragged to another block.
            function.department = department
            function.name = function_data["name"]
            if "subtitle" in function_data:
                function.subtitle = function_data["subtitle"]
            function.owners = function_data["owners"]
            function.level_1 = function_data["level_1"]
            function.level_2 = function_data["level_2"]
            function.sort_order = function_order
            _stamp(function, user)
            function.save()
            kept_function_ids.add(function.pk)

    dropped_functions = set(existing_functions) - kept_function_ids
    if dropped_functions:
        OrgFunction.objects.filter(pk__in=dropped_functions).delete()

    dropped_departments = set(existing_departments) - kept_department_ids
    if dropped_departments:
        # Cascades to any function still hanging off them.
        OrgDepartment.objects.filter(pk__in=dropped_departments).delete()

    return get_chart(company)
