from django.urls import path

from .views import (
    ApproveAPI,
    CancelAPI,
    DirectPushAPI,
    RejectAPI,
    RequestDetailAPI,
    RequestListCreateAPI,
    SapBomDetailAPI,
    SapBomListAPI,
    WorkflowAPI,
)

urlpatterns = [
    path("workflow/", WorkflowAPI.as_view(), name="bom-changes-workflow"),
    path("sap-boms/", SapBomListAPI.as_view(), name="bom-changes-sap-boms"),
    # ``path``: an item code may hold a slash; the trailing slash still ends it.
    path("sap-boms/<path:tree_code>/", SapBomDetailAPI.as_view(), name="bom-changes-sap-bom-detail"),
    path("requests/", RequestListCreateAPI.as_view(), name="bom-changes-requests"),
    path("requests/direct-push/", DirectPushAPI.as_view(), name="bom-changes-direct-push"),
    path("requests/<int:pk>/", RequestDetailAPI.as_view(), name="bom-changes-request-detail"),
    path("requests/<int:pk>/approve/", ApproveAPI.as_view(), name="bom-changes-approve"),
    path("requests/<int:pk>/reject/", RejectAPI.as_view(), name="bom-changes-reject"),
    path("requests/<int:pk>/cancel/", CancelAPI.as_view(), name="bom-changes-cancel"),
]
