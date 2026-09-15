"""Repoint the permanent-labour rows from the HR department tree to the
plant-wide ``accounts.Department`` master.

Permanent labour is the other half of the contractor register in
``labour_count``, and that register — like maintenance, the daily-needs gate and
the cost master before it — is kept against the shared ``accounts.Department``
list. Two masters meant the Permanent labour screen offered a department picker
that was empty on a plant whose departments were all sitting in the other table.

Existing rows hold HR department ids, which do not map to ``accounts`` ids, so
they are remapped the way ``maintenance.0012`` did it:

  1. add a temporary nullable FK to ``accounts.Department``,
  2. remap each row by matching the old department's name to an
     ``accounts.Department`` of the same name (creating it if missing),
  3. drop the old column and rename the temporary one into place.

Nullable throughout: a null department is the undivided bucket and stays null.

Irreversible on purpose. Going back would have to guess which company's HR
department a now-shared row belonged to, and a wrong guess files one plant's
headcount under another's.
"""

import django.db.models.deletion
from django.db import migrations, models


def remap_departments(apps, schema_editor):
    HrDepartment = apps.get_model("employee_hierarchy", "Department")
    OrgDepartment = apps.get_model("accounts", "Department")

    cache = {}

    def resolve(old_department_id):
        if old_department_id in cache:
            return cache[old_department_id]
        old = HrDepartment.objects.filter(pk=old_department_id).first()
        name = (old.name.strip() if old and old.name else "") or "General"
        department = OrgDepartment.objects.filter(name__iexact=name).first()
        if department is None:
            department = OrgDepartment.objects.create(name=name)
        cache[old_department_id] = department
        return department

    for model_name in (
        "PermanentLabourStrength",
        "PermanentLabourPresence",
        "PermanentLabourAudit",
    ):
        model = apps.get_model("employee_hierarchy", model_name)
        for row in model.objects.exclude(department__isnull=True).iterator():
            row.department_new = resolve(row.department_id)
            row.save(update_fields=["department_new"])


def _temp_field(on_delete, related_name):
    return models.ForeignKey(
        null=True,
        blank=True,
        on_delete=on_delete,
        related_name=related_name,
        to="accounts.department",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_cleanup_stale_permissions"),
        ("employee_hierarchy", "0004_permanent_labour_by_department"),
    ]

    operations = [
        # Every unique key and index that names the column -- including the two
        # "undivided" ones, whose ``department__isnull=True`` condition names it
        # just as surely -- comes off before the column does, and goes back on
        # after the replacement has been renamed into place. Left in place they
        # would be dropped with the column on PostgreSQL and survive only in
        # migration state, quietly costing the register its uniqueness guard.
        migrations.RemoveConstraint(
            model_name="permanentlabourstrength",
            name="uniq_permanent_strength_per_department",
        ),
        migrations.RemoveConstraint(
            model_name="permanentlabourstrength",
            name="uniq_permanent_strength_undivided",
        ),
        migrations.RemoveConstraint(
            model_name="permanentlabourpresence",
            name="uniq_permanent_presence_per_dept_shift",
        ),
        migrations.RemoveConstraint(
            model_name="permanentlabourpresence",
            name="uniq_permanent_presence_undivided_shift",
        ),
        migrations.RemoveIndex(
            model_name="permanentlabourpresence",
            name="employee_hi_company_ad298a_idx",
        ),
        migrations.RemoveIndex(
            model_name="permanentlabouraudit",
            name="employee_hi_company_fb9cf6_idx",
        ),
        migrations.AddField(
            model_name="permanentlabourstrength",
            name="department_new",
            field=_temp_field(
                django.db.models.deletion.PROTECT, "permanent_labour_strength_tmp"
            ),
        ),
        migrations.AddField(
            model_name="permanentlabourpresence",
            name="department_new",
            field=_temp_field(
                django.db.models.deletion.PROTECT, "permanent_labour_presence_tmp"
            ),
        ),
        migrations.AddField(
            model_name="permanentlabouraudit",
            name="department_new",
            field=_temp_field(
                django.db.models.deletion.SET_NULL, "permanent_labour_audit_tmp"
            ),
        ),
        migrations.RunPython(remap_departments, migrations.RunPython.noop),
        migrations.RemoveField(model_name="permanentlabourstrength", name="department"),
        migrations.RemoveField(model_name="permanentlabourpresence", name="department"),
        migrations.RemoveField(model_name="permanentlabouraudit", name="department"),
        migrations.RenameField(
            model_name="permanentlabourstrength",
            old_name="department_new",
            new_name="department",
        ),
        migrations.RenameField(
            model_name="permanentlabourpresence",
            old_name="department_new",
            new_name="department",
        ),
        migrations.RenameField(
            model_name="permanentlabouraudit",
            old_name="department_new",
            new_name="department",
        ),
        migrations.AlterField(
            model_name="permanentlabourstrength",
            name="department",
            field=models.ForeignKey(
                blank=True,
                help_text="Null means the figure is not split by department.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="permanent_labour_strength",
                to="accounts.department",
            ),
        ),
        migrations.AlterField(
            model_name="permanentlabourpresence",
            name="department",
            field=models.ForeignKey(
                blank=True,
                help_text="Null means the count is not split by department.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="permanent_labour_presence",
                to="accounts.department",
            ),
        ),
        migrations.AlterField(
            model_name="permanentlabouraudit",
            name="department",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="permanent_labour_audit",
                to="accounts.department",
            ),
        ),
        migrations.AddConstraint(
            model_name="permanentlabourstrength",
            constraint=models.UniqueConstraint(
                condition=models.Q(("department__isnull", False)),
                fields=("company", "department"),
                name="uniq_permanent_strength_per_department",
            ),
        ),
        migrations.AddConstraint(
            model_name="permanentlabourpresence",
            constraint=models.UniqueConstraint(
                condition=models.Q(("department__isnull", False)),
                fields=("company", "department", "work_date", "shift"),
                name="uniq_permanent_presence_per_dept_shift",
            ),
        ),
        migrations.AddConstraint(
            model_name="permanentlabourstrength",
            constraint=models.UniqueConstraint(
                condition=models.Q(("department__isnull", True)),
                fields=("company",),
                name="uniq_permanent_strength_undivided",
            ),
        ),
        migrations.AddConstraint(
            model_name="permanentlabourpresence",
            constraint=models.UniqueConstraint(
                condition=models.Q(("department__isnull", True)),
                fields=("company", "work_date", "shift"),
                name="uniq_permanent_presence_undivided_shift",
            ),
        ),
        migrations.AddIndex(
            model_name="permanentlabourpresence",
            index=models.Index(
                fields=["company", "department", "-work_date"],
                name="employee_hi_company_ad298a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="permanentlabouraudit",
            index=models.Index(
                fields=["company", "subject", "department", "-performed_at"],
                name="employee_hi_company_fb9cf6_idx",
            ),
        ),
    ]
