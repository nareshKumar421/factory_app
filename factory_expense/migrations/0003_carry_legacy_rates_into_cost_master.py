"""
Move whatever was entered into this app's short-lived rate tables into the
Cost Master, before 0004 drops them.

Those tables existed for a few hours on 01 Sep 2026 and a rate was already
entered against one of them. Dropping the tables without this step would throw
that away silently, which is the one outcome a migration must never produce.

The mapping is exact for the common case — an all-shifts rate is a Cost Master
row at the same scope, basis, amount and start date — so nothing is
approximated. The two places it cannot be exact are handled explicitly:

* **Shift.** Cost Master has no shift dimension. A DAY or NIGHT row cannot be
  represented, so it is carried at its scope with the shift recorded in
  ``notes``; where that would collide with another row for the same scope and
  date, the later one is left behind and named in ``notes`` on the survivor.
* **Salary month.** ``DepartmentSalaryConfig.month`` becomes ``effective_from``,
  which is the same thing said in Cost Master's vocabulary: the amount applies
  from the first of that month until a later row supersedes it. ``employee_count``
  has no home in a rate catalog and is dropped — the board no longer shows a
  per-employee figure.
"""

from django.db import migrations


def carry_over(apps, schema_editor):
    LabourRateConfig = apps.get_model("factory_expense", "LabourRateConfig")
    DepartmentSalaryConfig = apps.get_model("factory_expense", "DepartmentSalaryConfig")
    CostType = apps.get_model("cost_master", "CostType")
    CostRate = apps.get_model("cost_master", "CostRate")

    labour_type = CostType.objects.filter(code="factory-labour").first()
    salary_type = CostType.objects.filter(code="factory-salary").first()
    if labour_type is None or salary_type is None:
        # 0002 seeds both; if they are missing something is very wrong and
        # silently dropping the rows would be worse than failing loudly.
        raise RuntimeError("factory-labour / factory-salary cost types are missing.")

    for row in LabourRateConfig.objects.filter(is_active=True):
        scope = "DEPARTMENT" if row.department_id else "COMPANY"
        notes = row.notes or ""
        if row.shift != "ANY":
            notes = (notes + f" (was {row.shift}-shift only)").strip()

        exists = CostRate.objects.filter(
            cost_type=labour_type,
            scope=scope,
            company_id=row.company_id,
            department_id=row.department_id,
            effective_from=row.effective_from,
            is_active=True,
        ).first()
        if exists:
            # A same-scope, same-date row is already there — almost always the
            # DAY/NIGHT pair of a rate we just carried. Record it rather than
            # overwrite a rate somebody may have set deliberately.
            note = f" (a {row.shift}-shift rate of {row.rate_per_person_per_day} was not carried)"
            exists.notes = (exists.notes + note)[:200]
            exists.save(update_fields=["notes"])
            continue

        CostRate.objects.create(
            cost_type=labour_type,
            scope=scope,
            company_id=row.company_id,
            department_id=row.department_id,
            value_key="",
            basis="PER_PERSON_DAY",
            rate=row.rate_per_person_per_day,
            effective_from=row.effective_from,
            notes=notes[:200],
            is_active=True,
            created_by_id=row.created_by_id,
            updated_by_id=row.updated_by_id,
        )

    for row in DepartmentSalaryConfig.objects.filter(is_active=True):
        if CostRate.objects.filter(
            cost_type=salary_type,
            scope="DEPARTMENT",
            company_id=row.company_id,
            department_id=row.department_id,
            effective_from=row.month,
            is_active=True,
        ).exists():
            continue

        CostRate.objects.create(
            cost_type=salary_type,
            scope="DEPARTMENT",
            company_id=row.company_id,
            department_id=row.department_id,
            value_key="",
            basis="PER_MONTH",
            rate=row.monthly_amount,
            effective_from=row.month,
            notes=(row.notes or "")[:200],
            is_active=True,
            created_by_id=row.created_by_id,
            updated_by_id=row.updated_by_id,
        )


def noop_reverse(apps, schema_editor):
    """Nothing to undo: 0004's reverse recreates the empty tables, and the
    carried rows are now ordinary Cost Master rates that may have been edited
    since. Deleting them on a rollback would destroy live configuration."""


class Migration(migrations.Migration):

    dependencies = [("factory_expense", "0002_seed_factory_cost_types")]

    operations = [migrations.RunPython(carry_over, noop_reverse)]
