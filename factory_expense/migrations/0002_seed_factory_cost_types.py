"""
Register the two Cost Master types the wall board prices itself from.

Runs *before* the legacy rate tables are dropped, because 0003 copies their
contents into these types.

Deliberately separate from ``prod-*`` and ``blowing-*``: those price a
production run, and retuning a run's costing must not silently move the number
on the admin's wall.

No rates are created here — a rate is a business number an admin sets in
Admin › Cost Master. Until one exists the board says so on the tile rather than
implying the labour was free.
"""

from django.db import migrations

COST_TYPES = [
    {
        "code": "factory-labour",
        "name": "Factory — Contract Labour",
        "description": (
            "What one labourer costs for one day at the gate. Multiplied by the "
            "head count recorded in Gate › Labour In to price the Factory "
            "Expense board's labour line. Set a DEPARTMENT-scoped rate to "
            "override the factory-wide one for a single department."
        ),
        "default_basis": "PER_PERSON_DAY",
    },
    {
        "code": "factory-salary",
        "name": "Factory — Salary",
        "description": (
            "The monthly salary bill, one DEPARTMENT-scoped rate per department. "
            "The Factory Expense board spreads it evenly across the month's days, "
            "so a part-month view is an accrual rather than the whole bill landing "
            "on the 1st. A COMPANY-scoped rate is used only when no department "
            "rates exist, as a single unallocated line."
        ),
        "default_basis": "PER_MONTH",
    },
]


def seed(apps, schema_editor):
    CostType = apps.get_model("cost_master", "CostType")
    for spec in COST_TYPES:
        CostType.objects.update_or_create(
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "default_basis": spec["default_basis"],
                "is_credit": False,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    """Retire rather than delete — a CostType with rates cannot be removed, and
    a deleted code could later be reused for a different cost."""
    CostType = apps.get_model("cost_master", "CostType")
    CostType.objects.filter(
        code__in=[spec["code"] for spec in COST_TYPES]
    ).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("factory_expense", "0001_initial"),
        ("cost_master", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
