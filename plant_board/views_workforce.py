"""
plant_board/views_workforce.py

The one thing on this board an operator writes.

``GET     /api/v1/dashboards/plant-board/workforce/``
``PUT     /api/v1/dashboards/plant-board/workforce/``   figures
``POST    /api/v1/dashboards/plant-board/workforce/``   add a department
``DELETE  /api/v1/dashboards/plant-board/workforce/``   remove one that was added

Head count and monthly wage bill, per department. No system holds either: the
two department masters this app runs on are disjoint and neither contains the
departments the business reports on, and salary is withheld by the employee
endpoint from any login without a salary grant — which a wall-board login is.

WHY THE CATALOGUE IS ALWAYS RETURNED IN FULL
--------------------------------------------
``GET`` returns every department, configured or not, with nulls where nothing
has been typed. The settings page therefore renders the same rows on the first
visit as on the hundredth, and a department nobody has filled in is visibly
empty rather than absent. The alternative — returning only saved rows — makes a
new department look like a bug on the page that is supposed to fix it.

WHAT ``POST`` CAN AND CANNOT DO
-------------------------------
It adds a department the factory grew: a label, the band it reports under, and
whether its people are staff or hired labour. It cannot touch the six the board
was designed around. Those live in code precisely so that no amount of editing
on a settings page can move one to another band — a mistyped head count is a
mistyped head count, but a department silently re-banded changes what every
figure above it means. ``DELETE`` is bounded the same way: it removes a
department somebody added, and refuses a built-in.

WHY A BLANK FIELD IS SAVED, NOT SKIPPED
---------------------------------------
Clearing a figure is a real edit: it means "nobody has counted this any more",
and the board must go back to drawing a rule. So ``null`` is written as ``null``
rather than treated as "leave alone", and the only way to say "leave alone" is
not to send the department at all.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from stock_dashboard.models import PlantBoardWorkforce

from .constants import WORKFORCE_BANDS
from .permissions import CanViewPlantBoard
from .workforce import BUILT_IN, WORKFORCE_KINDS, departments, unique_key

logger = logging.getLogger(__name__)


def _payload(company_code: str):
    """Every department, with whatever has been typed against it."""
    return departments(company_code)


def _clean(value, field: str):
    """A number, or None. An empty string means the operator cleared it."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number.")
    if number < 0:
        raise ValueError(f"{field} cannot be negative.")
    return number


class PlantBoardWorkforceAPI(APIView):
    """Read and write the board's staffing figures."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPlantBoard]

    def get(self, request):
        company_code = request.company.company.code
        return Response({"data": _payload(company_code)})

    def put(self, request):
        company_code = request.company.company.code
        rows = request.data.get("departments")
        if not isinstance(rows, list):
            return Response(
                {"detail": "Send a `departments` list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        known = {entry["key"] for entry in departments(company_code)}
        for row in rows:
            key = (row or {}).get("key")
            if key not in known:
                # Refused rather than stored: a key that resolves to no
                # department has no band and no kind, so the board could never
                # show it.
                return Response(
                    {"detail": f"Unknown department '{key}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                people = _clean(row.get("employees"), "Employees")
                salary = _clean(row.get("salary_monthly"), "Salary")
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            # `defaults` names only the figures, so an added department's own
            # label, band and kind survive an edit of its head count. They are
            # its identity, not one of the numbers on the form.
            PlantBoardWorkforce.objects.update_or_create(
                company_code=company_code,
                department=key,
                defaults={
                    "employees": None if people is None else int(people),
                    "salary_monthly": salary,
                    "updated_by": request.user if request.user.is_authenticated else None,
                },
            )

        logger.info(
            "plant_board: workforce updated for %s by %s",
            company_code,
            getattr(request.user, "username", "anonymous"),
        )
        return Response({"data": _payload(company_code)})

    def post(self, request):
        """Add a department this plant grew.

        The label is the only free text; band and kind are closed lists, because
        they are what the board reads to decide which strip the people appear on
        and which half of it they count towards. A typo in either would put the
        department somewhere nobody looks rather than fail.
        """
        company_code = request.company.company.code
        label = str(request.data.get("label") or "").strip()
        band = str(request.data.get("band") or "").strip()
        kind = str(request.data.get("kind") or "").strip()

        if not label:
            return Response(
                {"detail": "Give the department a name."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if band not in WORKFORCE_BANDS:
            return Response(
                {"detail": f"Band must be one of: {', '.join(WORKFORCE_BANDS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if kind not in WORKFORCE_KINDS:
            return Response(
                {"detail": f"Kind must be one of: {', '.join(WORKFORCE_KINDS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = {entry["key"] for entry in departments(company_code)}
        labels = {
            entry["label"].strip().lower() for entry in departments(company_code)
        }
        if label.lower() in labels:
            # Two departments with one name on a wall is worse than a refusal:
            # the strip would show the same caption twice with different figures.
            return Response(
                {"detail": f"A department called '{label}' already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        key = unique_key(label, existing)
        try:
            people = _clean(request.data.get("employees"), "Employees")
            salary = _clean(request.data.get("salary_monthly"), "Salary")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        PlantBoardWorkforce.objects.create(
            company_code=company_code,
            department=key,
            label=label,
            band=band,
            kind=kind,
            employees=None if people is None else int(people),
            salary_monthly=salary,
            updated_by=request.user if request.user.is_authenticated else None,
        )
        logger.info(
            "plant_board: workforce department '%s' added to %s band for %s by %s",
            label, band, company_code, getattr(request.user, "username", "anonymous"),
        )
        return Response({"data": _payload(company_code)}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        """Remove a department that was added here. Never a built-in."""
        company_code = request.company.company.code
        key = str(request.data.get("key") or request.query_params.get("key") or "").strip()

        if key in BUILT_IN:
            return Response(
                {
                    "detail": (
                        "This department is part of the board's own layout and "
                        "cannot be removed here."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        row = PlantBoardWorkforce.objects.filter(
            company_code=company_code, department=key
        ).exclude(band="").first()
        if row is None:
            return Response(
                {"detail": f"No added department '{key}'."},
                status=status.HTTP_404_NOT_FOUND,
            )

        row.delete()
        logger.info(
            "plant_board: workforce department '%s' removed from %s by %s",
            key, company_code, getattr(request.user, "username", "anonymous"),
        )
        return Response({"data": _payload(company_code)})
