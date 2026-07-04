"""Fold Zones into Purposes.

The Warehouse Ops UI dropped the Zone layer in favour of a single per-cell
Purpose. This data migration preserves existing zone groupings: for every zone
it creates (or reuses) a same-named Purpose that holds stock, assigns it to the
cells that were grouped only by that zone, and detaches the now-unused zoneId.

Non-destructive: it never deletes records (zone rows are left in place, dormant)
and never overwrites a cell that already has a purpose. Runs once per company /
warehouse via the normal deploy (`manage.py migrate`); a no-op when there are no
zones.
"""
import uuid

from django.db import migrations
from django.utils import timezone


def _norm(value):
    return (value or '').strip().lower()


def fold_zones_into_purposes(apps, schema_editor):
    Zone = apps.get_model('wms', 'Zone')
    CellPurpose = apps.get_model('wms', 'CellPurpose')
    Location = apps.get_model('wms', 'Location')

    now = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    for zone in Zone.objects.all():
        zd = zone.data or {}
        zone_id = zd.get('id')
        if not zone_id:
            continue
        company = zone.company
        warehouse_id = zd.get('warehouseId')

        # Reuse a same-named purpose in this warehouse if one exists, else create it.
        purpose_id = None
        for purpose in CellPurpose.objects.filter(company=company, data__warehouseId=warehouse_id):
            if _norm((purpose.data or {}).get('name')) == _norm(zd.get('name')):
                purpose_id = (purpose.data or {}).get('id')
                break
        if not purpose_id:
            purpose_id = str(uuid.uuid4())
            name = (zd.get('name') or 'Zone').strip()
            code = (zd.get('code') or '').strip() or name.upper().replace(' ', '-')
            CellPurpose.objects.create(
                company=company,
                record_id=purpose_id,
                data={
                    'id': purpose_id,
                    'warehouseId': warehouse_id,
                    'code': code,
                    'name': name,
                    'color': zd.get('color') or '#2563eb',
                    'holdsStock': True,
                    'enabled': True,
                    'createdAt': now,
                    'updatedAt': now,
                },
            )

        # Give zone-only cells the purpose; detach the zone reference either way.
        for loc in Location.objects.filter(company=company, data__zoneId=zone_id):
            data = loc.data or {}
            if not data.get('purposeId'):
                data['purposeId'] = purpose_id
            data['zoneId'] = None
            data['updatedAt'] = now
            loc.data = data
            loc.save(update_fields=['data', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('wms', '0004_cellpurpose'),
    ]

    operations = [
        migrations.RunPython(fold_zones_into_purposes, migrations.RunPython.noop),
    ]
