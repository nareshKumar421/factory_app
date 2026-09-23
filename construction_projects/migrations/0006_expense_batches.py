"""Expenses are approved in batches, not one at a time.

`Expense.status` / `decided_by` / `decided_at` / `decision_note` move up to a
new `ExpenseBatch`: lines pile into the project's open batch and one decision
settles the lot.

Order matters. The FK is non-nullable in the end state, so it is added nullable,
backfilled, and only then enforced — and the old per-line columns are dropped
last, after their content has been used to decide which batch each line belongs
in.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def group_into_batches(apps, schema_editor):
    """Give every project with expenses a batch, preserving what was decided.

    Previously-approved lines go into a settled batch; everything else --
    pending, and anything that had been disallowed -- goes into one open batch
    for the site to send again. A disallowed line comes back as unapproved
    rather than vanishing, because the money was still spent and somebody has
    to account for it.
    """
    Project = apps.get_model("construction_projects", "Project")
    ExpenseBatch = apps.get_model("construction_projects", "ExpenseBatch")
    Expense = apps.get_model("construction_projects", "Expense")

    for project in Project.objects.all():
        expenses = list(project.expenses.all())
        if not expenses:
            continue

        approved = [e for e in expenses if e.status == "APPROVED"]
        rest = [e for e in expenses if e.status != "APPROVED"]
        next_no = 1

        if approved:
            batch = ExpenseBatch.objects.create(
                project=project,
                batch_no=next_no,
                status="APPROVED",
                decided_at=max(
                    (e.decided_at for e in approved if e.decided_at), default=None
                ),
                decision_note="Carried over from per-line approval.",
                is_active=True,
            )
            next_no += 1
            Expense.objects.filter(id__in=[e.id for e in approved]).update(batch=batch)

        if rest:
            batch = ExpenseBatch.objects.create(
                project=project, batch_no=next_no, status="OPEN", is_active=True
            )
            Expense.objects.filter(id__in=[e.id for e in rest]).update(batch=batch)


def ungroup(apps, schema_editor):
    """Put each line's decision back on the line itself."""
    Expense = apps.get_model("construction_projects", "Expense")
    for expense in Expense.objects.select_related("batch"):
        batch = expense.batch
        expense.status = "APPROVED" if batch and batch.status == "APPROVED" else "PENDING"
        if batch:
            expense.decided_at = batch.decided_at
            expense.decided_by_id = batch.decided_by_id
            expense.decision_note = batch.decision_note
        expense.save(
            update_fields=["status", "decided_at", "decided_by", "decision_note"]
        )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("construction_projects", "0005_area_and_estimate_lines"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExpenseBatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                ("batch_no", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("OPEN", "Collecting"),
                            ("SUBMITTED", "Awaiting approval"),
                            ("APPROVED", "Approved"),
                            ("RETURNED", "Sent back"),
                        ],
                        db_index=True,
                        default="OPEN",
                        max_length=10,
                    ),
                ),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_note", models.CharField(blank=True, default="", max_length=255)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="expensebatch_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="expensebatch_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="construction_batches_decided",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="construction_batches_submitted",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expense_batches",
                        to="construction_projects.project",
                    ),
                ),
            ],
            options={
                "verbose_name": "Expense Batch",
                "verbose_name_plural": "Expense Batches",
                "ordering": ["project_id", "-batch_no"],
                "default_permissions": (),
                "unique_together": {("project", "batch_no")},
            },
        ),
        migrations.AddConstraint(
            model_name="expensebatch",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_active", True), ("status__in", ["OPEN", "RETURNED"])
                ),
                fields=("project",),
                name="uniq_open_expense_batch_per_project",
            ),
        ),
        # Nullable first, so existing rows survive the add.
        migrations.AddField(
            model_name="expense",
            name="batch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="expenses",
                to="construction_projects.expensebatch",
            ),
        ),
        migrations.RunPython(group_into_batches, ungroup),
        # Now that every row has one, enforce it.
        migrations.AlterField(
            model_name="expense",
            name="batch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="expenses",
                to="construction_projects.expensebatch",
            ),
        ),
        migrations.RemoveIndex(
            model_name="expense", name="constructio_project_ead507_idx"
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["batch"], name="constructio_batch_i_2a4f1c_idx"),
        ),
        # The per-line decision, dropped only after it has been used above.
        migrations.RemoveField(model_name="expense", name="status"),
        migrations.RemoveField(model_name="expense", name="decided_by"),
        migrations.RemoveField(model_name="expense", name="decided_at"),
        migrations.RemoveField(model_name="expense", name="decision_note"),
    ]
