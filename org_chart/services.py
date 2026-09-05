"""
Reading and writing the ownership chart.

The page saves the chart WHOLE, not row by row. Editing it is a sit-down job —
somebody renames a function, moves a row, adds two names and drops one — and a
single atomic save is both what the screen means by "Save" and the only way to
let names swap without a half-applied chart in between. The write is a diff, so
rows that were only moved keep their ids (and their created/updated stamps)
instead of being recreated.
"""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import OrgDepartment, OrgFunction

#: Raised as-is when the chart changed underneath the editor.
STALE_MESSAGE = (
    "The chart changed since this page was opened. Reload it and re-apply your edits."
)


def get_chart():
    """Every department with its functions, in chart order."""
    return list(
        OrgDepartment.objects.prefetch_related("functions").order_by("sort_order", "name")
    )


def _stamp(instance, user):
    if user is not None and getattr(user, "is_authenticated", False):
        instance.updated_by = user
        if instance.pk is None:
            instance.created_by = user


@transaction.atomic
def save_chart(departments_data, user=None):
    """Replace the chart with ``departments_data``; return the saved chart.

    ``departments_data`` is the validated output of
    :class:`~org_chart.serializers.ChartSaveSerializer`. Rows carrying an ``id``
    are updated in place, rows without one are created, and anything the payload
    no longer mentions is deleted — the editor always sends the complete chart.
    """
    existing_departments = {d.pk: d for d in OrgDepartment.objects.all()}
    existing_functions = {f.pk: f for f in OrgFunction.objects.all()}

    kept_department_ids, kept_function_ids = set(), set()

    for order, data in enumerate(departments_data):
        department_id = data.get("id")
        if department_id:
            department = existing_departments.get(department_id)
            if department is None:
                raise ValidationError(STALE_MESSAGE)
        else:
            department = OrgDepartment()
        department.name = data["name"]
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

    return get_chart()
