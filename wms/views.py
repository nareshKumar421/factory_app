"""
REST API for the Warehouse Ops (WMS) module.

One uniform CRUD surface serves all nine collections, matching the frontend
storage-adapter contract (``FactoryFlow`` ``src/modules/wms/storage/apiAdapter.ts``):

    GET    /api/v1/wms/<collection>/            -> list
    POST   /api/v1/wms/<collection>/            -> create (upsert by id)
    POST   /api/v1/wms/<collection>/bulk/       -> bulk create (upsert)
    GET    /api/v1/wms/<collection>/<id>/       -> get (404 -> treated as null)
    PATCH  /api/v1/wms/<collection>/<id>/       -> partial update (deep merge)
    DELETE /api/v1/wms/<collection>/<id>/       -> delete

Records are opaque camelCase JSON documents authored by the frontend; we persist
them verbatim and always return the stored document so the round-trip is lossless.
Every request is scoped to the caller's company (see ``HasCompanyContext``), so
companies never see each other's warehouses.

Authorization is enforced by ``WmsCollectionPermission`` (see ``permissions.py``):
reads are open; writes require the matching ``wms.<add|change|delete>_<model>``
permission granted via the ``WMS Admin`` / ``WMS Operator`` groups.
"""
import logging
import uuid

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import CellPurpose, Location, Zone
from .permissions import COLLECTION_MODELS, WmsCollectionPermission

logger = logging.getLogger(__name__)

# Collections whose records belong to a single warehouse — each carries that
# warehouse's id in its ``warehouseId`` document field. Deleting a warehouse
# cascades to these so its grid never outlives it as orphan rows: ``warehouseId``
# is a plain JSON field, not a DB foreign key, so nothing is cleaned up on its
# own. (Pallets/inventory/movements are scoped to *locations*, not directly to a
# warehouse, and are live stock / append-only audit data — left untouched here.)
WAREHOUSE_SCOPED_MODELS = (Zone, CellPurpose, Location)


def _resolve_model(collection):
    """Return the model for a collection, or None for an unknown name."""
    return COLLECTION_MODELS.get(collection)


def _company(request):
    """The Company the authenticated request is acting within."""
    return request.company.company


def _record_id(record):
    """The client-supplied id, generating one if (unusually) absent."""
    rid = record.get('id') if isinstance(record, dict) else None
    if not rid:
        rid = str(uuid.uuid4())
        record['id'] = rid
    return str(rid)


def _deep_merge(base, patch):
    """Recursively merge ``patch`` into ``base``; nested dicts merge, everything
    else (lists, scalars) replaces. Avoids the data loss of a shallow merge where
    a partial nested object (capacity, materialRules, namingScheme…) would wipe
    its sibling keys."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    merged = dict(base)
    for key, value in patch.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _upsert(model, company, record):
    """Create or replace a record document, keyed by (company, id)."""
    record_id = _record_id(record)
    obj, _created = model.objects.update_or_create(
        company=company,
        record_id=record_id,
        defaults={'data': record},
    )
    return obj


class _WmsBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, WmsCollectionPermission]

    def get_model_or_404(self, collection):
        model = _resolve_model(collection)
        if model is None:
            return None, Response(
                {'error': f"Unknown WMS collection '{collection}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return model, None


class WmsCollectionAPI(_WmsBaseView):
    """List a collection (GET) or create/upsert a single record (POST)."""

    def get(self, request, collection):
        model, err = self.get_model_or_404(collection)
        if err:
            return err
        qs = model.objects.filter(company=_company(request)).order_by('created_at')

        # Optional, backward-compatible warehouse scoping. The per-warehouse
        # collections (locations, zones, ...) carry a ``warehouseId`` inside their JSON
        # document. A warehouse-detail screen needs only its own warehouse's rows, so
        # ``?warehouseId=<id>`` filters server-side instead of shipping every warehouse's
        # rows for the whole company (Jivo Oil had 5,197 locations / ~4 MB across 3
        # warehouses). No param -> full list, so the storage-adapter contract is unchanged.
        warehouse_id = request.query_params.get('warehouseId')
        if warehouse_id:
            qs = qs.filter(data__warehouseId=warehouse_id)

        # Optional, backward-compatible pagination: with no ?limit the full list
        # is returned as a plain array (what the storage adapter expects); with
        # ?limit a {results,count,offset,limit} page is returned for large
        # collections (e.g. movements/inventory).
        limit = request.query_params.get('limit')
        if limit is not None:
            try:
                limit = max(0, int(limit))
                offset = max(0, int(request.query_params.get('offset') or 0))
            except (TypeError, ValueError):
                return Response({'error': 'limit/offset must be integers.'},
                                status=status.HTTP_400_BAD_REQUEST)
            total = qs.count()
            rows = qs[offset:offset + limit]
            return Response({
                'results': [row.data for row in rows],
                'count': total,
                'offset': offset,
                'limit': limit,
            })

        return Response([row.data for row in qs])

    def post(self, request, collection):
        model, err = self.get_model_or_404(collection)
        if err:
            return err
        record = request.data
        if not isinstance(record, dict):
            return Response(
                {'error': 'Expected a single record object.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        obj = _upsert(model, _company(request), record)
        return Response(obj.data, status=status.HTTP_201_CREATED)


class WmsBulkCreateAPI(_WmsBaseView):
    """Create/upsert many records in one transaction."""

    def post(self, request, collection):
        model, err = self.get_model_or_404(collection)
        if err:
            return err
        records = request.data
        if not isinstance(records, list):
            return Response(
                {'error': 'Expected a list of records.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        company = _company(request)
        with transaction.atomic():
            saved = [_upsert(model, company, rec).data for rec in records
                     if isinstance(rec, dict)]
        return Response(saved, status=status.HTTP_201_CREATED)


class WmsRecordAPI(_WmsBaseView):
    """Fetch (GET), merge-update (PATCH), or delete (DELETE) one record."""

    def _get_object(self, request, collection, record_id):
        model, err = self.get_model_or_404(collection)
        if err:
            return None, err
        obj = model.objects.filter(
            company=_company(request), record_id=str(record_id)
        ).first()
        if obj is None:
            return None, Response(status=status.HTTP_404_NOT_FOUND)
        return obj, None

    def get(self, request, collection, record_id):
        obj, err = self._get_object(request, collection, record_id)
        if err:
            return err
        return Response(obj.data)

    def patch(self, request, collection, record_id):
        obj, err = self._get_object(request, collection, record_id)
        if err:
            return err
        patch = request.data
        if not isinstance(patch, dict):
            return Response(
                {'error': 'Expected a partial record object.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        merged = _deep_merge(obj.data or {}, patch)
        merged['id'] = obj.record_id  # id is immutable
        obj.data = merged
        obj.save(update_fields=['data', 'updated_at'])
        return Response(obj.data)

    def delete(self, request, collection, record_id):
        obj, err = self._get_object(request, collection, record_id)
        if err:
            return err
        company = _company(request)
        with transaction.atomic():
            obj.delete()
            # Deleting a warehouse cascades to everything it owns, so its zones,
            # cell purposes and locations never linger as orphan rows.
            if collection == 'warehouses':
                for model in WAREHOUSE_SCOPED_MODELS:
                    model.objects.filter(
                        company=company, data__warehouseId=str(record_id)
                    ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
