from django.urls import path
from . import views

urlpatterns = [
    # Shipment CRUD
    path("shipments/", views.ShipmentListAPI.as_view(), name="outbound-shipment-list"),
    path("shipments/sync/", views.ShipmentSyncAPI.as_view(), name="outbound-sync"),
    path("shipments/<int:shipment_id>/", views.ShipmentDetailAPI.as_view(), name="outbound-shipment-detail"),

    # Dock bay
    path("shipments/<int:shipment_id>/assign-bay/", views.AssignBayAPI.as_view(), name="outbound-assign-bay"),

    # Picking
    path("shipments/<int:shipment_id>/pick-tasks/", views.PickTaskListAPI.as_view(), name="outbound-pick-tasks"),
    path("shipments/<int:shipment_id>/generate-picks/", views.GeneratePicksAPI.as_view(), name="outbound-generate-picks"),
    path("pick-tasks/<int:task_id>/", views.PickTaskUpdateAPI.as_view(), name="outbound-pick-task-update"),
    path("pick-tasks/<int:task_id>/scan/", views.PickTaskScanAPI.as_view(), name="outbound-pick-task-scan"),

    # Pack & Stage
    path("shipments/<int:shipment_id>/confirm-pack/", views.ConfirmPackAPI.as_view(), name="outbound-confirm-pack"),
    path("shipments/<int:shipment_id>/stage/", views.StageShipmentAPI.as_view(), name="outbound-stage"),

    # Vehicle & Inspection
    path("shipments/<int:shipment_id>/link-vehicle/", views.LinkVehicleAPI.as_view(), name="outbound-link-vehicle"),
    path("shipments/<int:shipment_id>/inspect-trailer/", views.InspectTrailerAPI.as_view(), name="outbound-inspect-trailer"),

    # Loading & Dispatch
    path("shipments/<int:shipment_id>/load/", views.LoadShipmentAPI.as_view(), name="outbound-load"),
    path("shipments/<int:shipment_id>/supervisor-confirm/", views.SupervisorConfirmAPI.as_view(), name="outbound-supervisor-confirm"),
    path("shipments/<int:shipment_id>/generate-bol/", views.GenerateBOLAPI.as_view(), name="outbound-generate-bol"),
    path("shipments/<int:shipment_id>/dispatch/", views.DispatchShipmentAPI.as_view(), name="outbound-dispatch"),

    # Goods Issue
    path("shipments/<int:shipment_id>/goods-issue/", views.GoodsIssueStatusAPI.as_view(), name="outbound-goods-issue"),
    path("shipments/<int:shipment_id>/goods-issue/retry/", views.GoodsIssueRetryAPI.as_view(), name="outbound-goods-issue-retry"),

    # Dashboard
    path("dashboard/", views.OutboundDashboardAPI.as_view(), name="outbound-dashboard"),
]
