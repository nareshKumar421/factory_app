from django.db import migrations


def reset_orphaned(apps, schema_editor):
    """
    Runs that requested preform under the old blowing-local system (before the
    warehouse-BOMRequest unification) keep a stale warehouse_approval_status but
    have no warehouse.BOMRequest. Reset them to NOT_REQUESTED so they can be
    re-requested into the warehouse queue.
    """
    BlowingRun = apps.get_model('blowing', 'BlowingRun')
    BOMRequest = apps.get_model('warehouse', 'BOMRequest')

    stuck = BlowingRun.objects.exclude(warehouse_approval_status='NOT_REQUESTED')
    for run in stuck:
        if not BOMRequest.objects.filter(blowing_run_id=run.id).exists():
            run.warehouse_approval_status = 'NOT_REQUESTED'
            run.save(update_fields=['warehouse_approval_status'])


class Migration(migrations.Migration):
    dependencies = [
        ('blowing', '0010_delete_blowingpreformrequest'),
        ('warehouse', '0012_bomrequest_blowing_run_and_more'),
    ]
    operations = [
        migrations.RunPython(reset_orphaned, migrations.RunPython.noop),
    ]
