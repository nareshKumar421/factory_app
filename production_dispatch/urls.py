from django.urls import path

from .views import ProductionDispatchDocumentsAPI, ProductionDispatchReportAPI

app_name = "production_dispatch"

urlpatterns = [
    path("report/", ProductionDispatchReportAPI.as_view(), name="production-dispatch-report"),
    path("documents/", ProductionDispatchDocumentsAPI.as_view(), name="production-dispatch-documents"),
]
