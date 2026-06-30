from django.urls import path

from .views import (
    DispatchBillByNumberAPI,
    DispatchBillListAPI,
    DispatchPipelineView,
    DispatchPlanUpdateAPI,
    DispatchScheduleItemsAPI,
    DispatchScheduleListAPI,
)

urlpatterns = [
    path("bills/", DispatchBillListAPI.as_view(), name="dispatch-plan-bills"),
    path("pipeline/", DispatchPipelineView.as_view(), name="dispatch-plan-pipeline"),
    path("schedule/", DispatchScheduleListAPI.as_view(), name="dispatch-plan-schedule"),
    path(
        "schedule/<int:doc_entry>/items/",
        DispatchScheduleItemsAPI.as_view(),
        name="dispatch-plan-schedule-items",
    ),
    path(
        "bills/by-number/<str:invoice_number>/",
        DispatchBillByNumberAPI.as_view(),
        name="dispatch-plan-bill-by-number",
    ),
    path(
        "bills/<int:sap_invoice_doc_entry>/plan/",
        DispatchPlanUpdateAPI.as_view(),
        name="dispatch-plan-update",
    ),
]

