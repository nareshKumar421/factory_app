"""
REST API for the Warehouse Ops (WMS) module.

One uniform CRUD surface serves all nine collections, matching the frontend
storage-adapter contract (``FactoryFlow`` ``src/modules/wms/storage/apiAdapter.ts``):

    GET    /api/v1/wms/<collection>/            -> list
    POST   /api/v1/wms/<collection>/            -> create (upsert by id)
    POST   /api/v1/wms/<collection>/bulk/       -> bulk create (upsert)
    GET    /api/v1/wms/<collection>/<id>/       -> get (404 -> treated as null)
    PATCH  /api/v1/wms/<collection>/<id>/       -> partial update (merge)
    DELETE /api/v1/wms/<collection>/<id>/       -> delete

Records are opaque camelCase JSON documents authored by the frontend; we persist
them verbatim and always return the stored document so the round-trip is lossless.
Every request is scoped to the caller's company (see ``HasCompanyContext``), so
companies never see each other's warehouses.
"""
import logging
import uuid

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    Inventory, Location, Material, Movement, Pallet, Settings, Template,
    Warehouse, Zone,
)

logger = logging.getLogger(__name__)


# Maps the URL collection segment to its model. The keys MUST stay in sync with
# WMS_COLLECTIONS in the frontend adapter.types.ts.
COLLECTION_MODELS = {
    'warehouses': Warehouse,
    'zones': Zone,
    'locations': Location,
    'materials': Material,
    'pallets': Pallet,
    'inventory': Inventory,
    'movements': Movement,
    'templates': Template,
    'settings': Settings,
}


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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
        rows = model.objects.filter(company=_company(request)).order_by('created_at')
        return Response([row.data for row in rows])

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
        merged = {**(obj.data or {}), **patch}
        merged['id'] = obj.record_id  # id is immutable
        obj.data = merged
        obj.save(update_fields=['data', 'updated_at'])
        return Response(obj.data)

    def delete(self, request, collection, record_id):
        obj, err = self._get_object(request, collection, record_id)
        if err:
            return err
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
