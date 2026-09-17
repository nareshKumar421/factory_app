"""
Department becomes Branch.

The cash book was filing payments against ``accounts.Department`` -- the whole
company's list (IT, Ecom, Store, Mess, two rows both called "production"), far
wider and looser than a cash box ever spends against. It now files against its
own :class:`~cash_book.models.CashBranch`, of which there are four: Oil,
Beverage, Water and Common.

The four are created here for every company rather than left to a setup
command, so a database that migrates is a database that works -- an entry form
with an empty branch picker cannot record a payment at all. They are editable
afterwards from Settings -> Cash Book Branches.

Existing entries are carried across by name (see ``BRANCH_OF``). Nothing is
dropped until every row has been moved, and the move is checked: if a payment
would be left without a branch the migration raises rather than quietly
loosening a rule the rest of the module enforces.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

#: Old department name (lower-cased) -> new branch. Everything else, including
#: a blank, goes to Common: a spend nobody assigned is a common spend.
BRANCH_OF = {
    "canola": "Oil",
    "oil": "Oil",
    "wg": "Beverage",
    "beverage": "Beverage",
    "beverages": "Beverage",
    "water": "Water",
    "mart": "Common",
    "common": "Common",
}

DEFAULT_BRANCHES = ("Oil", "Beverage", "Water", "Common")
FALLBACK_BRANCH = "Common"


def create_branches_and_move_entries(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    CashBranch = apps.get_model("cash_book", "CashBranch")
    CashEntry = apps.get_model("cash_book", "CashEntry")

    branches = {}
    for company in Company.objects.all():
        for order, name in enumerate(DEFAULT_BRANCHES):
            branch, _ = CashBranch.objects.get_or_create(
                company=company, name=name, defaults={"sort_order": order}
            )
            branches[(company.id, name)] = branch

    for entry in CashEntry.objects.select_related("department").iterator():
        if entry.department_id is None:
            continue  # a receipt: belongs to no branch, by design
        name = BRANCH_OF.get(
            (entry.department.name or "").strip().lower(), FALLBACK_BRANCH
        )
        entry.branch = branches[(entry.company_id, name)]
        entry.save(update_fields=["branch"])

    stranded = CashEntry.objects.filter(
        direction="OUT", branch__isnull=True
    ).count()
    if stranded:
        raise RuntimeError(
            f"{stranded} payment(s) would be left without a branch. "
            f"Nothing has been migrated."
        )


def restore_departments(apps, schema_editor):
    """Reversing drops the branch link; the old departments are long gone.

    Deliberately a no-op rather than a guess. Rolling this back leaves entries
    with no department, which is what the column was before it was populated,
    and re-running forwards re-derives everything from ``BRANCH_OF``.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('cash_book', '0001_initial'),
        ('company', '0003_alter_usercompany_role'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CashBranch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_active', models.BooleanField(default=True)),
                ('name', models.CharField(max_length=60)),
                ('sort_order', models.PositiveSmallIntegerField(default=0, help_text='Position in the picker. Ties fall back to name.')),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cash_branches', to='company.company')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_created', to=settings.AUTH_USER_MODEL)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_updated', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name_plural': 'Cash branches',
                'ordering': ['sort_order', 'name'],
                'permissions': [('can_manage_cash_branches', 'Can add, rename and retire cash book branches')],
            },
        ),
        migrations.AddField(
            model_name='cashentry',
            name='branch',
            field=models.ForeignKey(blank=True, help_text='Which branch the money was spent for. Required on a payment; a cash receipt into the box belongs to no branch.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='cash_entries', to='cash_book.cashbranch'),
        ),
        migrations.AddConstraint(
            model_name='cashbranch',
            constraint=models.UniqueConstraint(fields=('company', 'name'), name='uq_cash_branch_company_name'),
        ),
        # Only now that every entry has a branch is the old column dropped.
        migrations.RunPython(
            create_branches_and_move_entries, restore_departments
        ),
        migrations.RemoveField(
            model_name='cashentry',
            name='department',
        ),
    ]
