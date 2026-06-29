from django.urls import path

from .views import WmsBulkCreateAPI, WmsCollectionAPI, WmsRecordAPI

# NOTE: the `bulk/` route is declared before `<record_id>/` so it is never
# swallowed as a record id.
urlpatterns = [
    path('<str:collection>/', WmsCollectionAPI.as_view(), name='wms-collection'),
    path('<str:collection>/bulk/', WmsBulkCreateAPI.as_view(), name='wms-bulk'),
    path('<str:collection>/<str:record_id>/', WmsRecordAPI.as_view(), name='wms-record'),
]
