from django.urls import path

from .views import (
    AlarmPreviewAPI,
    AlarmSendAPI,
    CapacityCheckAPI,
    FloorAuditAPI,
    FloorConventionAPI,
    LeadTimeListAPI,
    MachineCapacityListAPI,
    MaterialMachineMapListAPI,
    ProcurementAlarmsAPI,
    ReferenceImportHistoryAPI,
    ReferenceTemplateUploadAPI,
    SupplyChainDashboardAPI,
    SupplyChainPolicyAPI,
)

app_name = "supply_chain"

urlpatterns = [
    path("dashboard/", SupplyChainDashboardAPI.as_view(), name="sc-dashboard"),
    path("procurement/", ProcurementAlarmsAPI.as_view(), name="sc-procurement"),
    path("capacity/", CapacityCheckAPI.as_view(), name="sc-capacity"),
    path("policy/", SupplyChainPolicyAPI.as_view(), name="sc-policy"),
    path("floors/", FloorAuditAPI.as_view(), name="sc-floors"),
    path("floor-convention/", FloorConventionAPI.as_view(), name="sc-floor-convention"),
    path("alarms/preview/", AlarmPreviewAPI.as_view(), name="sc-alarm-preview"),
    path("alarms/send/", AlarmSendAPI.as_view(), name="sc-alarm-send"),
    path("reference/lead-times/", LeadTimeListAPI.as_view(), name="sc-lead-times"),
    path("reference/machines/", MachineCapacityListAPI.as_view(), name="sc-machines"),
    path("reference/sku-machines/", MaterialMachineMapListAPI.as_view(), name="sc-sku-machines"),
    path("reference/upload/", ReferenceTemplateUploadAPI.as_view(), name="sc-reference-upload"),
    path("reference/imports/", ReferenceImportHistoryAPI.as_view(), name="sc-reference-imports"),
]
