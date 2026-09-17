"""Split the credit-note permissions per family (A/R vs A/P).

`0029` shipped one pair covering both families. A/R credit notes credit a
customer and A/P ones debit a vendor — different documents worked by different
people, and in SAP the two queues' authorizers do not overlap by a single
account — so the pair becomes two.

``AlterModelOptions`` creates the four new permissions but leaves the two it
replaced behind: Django never deletes a permission row for a codename that has
gone away. They are removed here, and only if nothing holds them, so this can
never quietly strip someone's access.
"""

from django.db import migrations

SUPERSEDED = ["can_view_credit_note_approval", "can_approve_credit_note"]


def drop_superseded_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    for codename in SUPERSEDED:
        perm = Permission.objects.filter(
            content_type__app_label="warehouse", codename=codename
        ).first()
        if perm is None:
            continue
        # Held by anyone? Then leave it and let a human decide — an orphan row
        # is harmless, silently revoking access is not.
        if perm.group_set.exists() or perm.user_set.exists():
            print(
                f"  ! keeping warehouse.{codename}: still held by "
                f"{perm.group_set.count()} group(s) and {perm.user_set.count()} user(s). "
                "Move them onto the A/R and A/P permissions, then delete it."
            )
            continue
        perm.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('warehouse', '0029_creditnoteapprovalaudit'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='creditnoteapprovalaudit',
            options={'default_permissions': (), 'ordering': ['-created_at'], 'permissions': [('can_view_ar_credit_note_approval', 'Can view the SAP A/R credit-note queue'), ('can_approve_ar_credit_note', 'Can approve or reject a SAP A/R credit note'), ('can_view_ap_credit_note_approval', 'Can view the SAP A/P credit-note queue'), ('can_approve_ap_credit_note', 'Can approve or reject a SAP A/P credit note')]},
        ),
        # Reversing only restores the old Meta; the next `migrate` recreates
        # whatever permissions that Meta names, so the no-op reverse is honest.
        migrations.RunPython(drop_superseded_permissions, migrations.RunPython.noop),
    ]
