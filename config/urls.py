"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path,include
from django.conf.urls.static import static
from django.utils.html import mark_safe
from .view import RootApiView

# Configure admin site branding from settings
admin.site.site_header = mark_safe(settings.ADMIN_SITE_HEADER)
admin.site.site_title = settings.ADMIN_SITE_TITLE
admin.site.index_title = settings.ADMIN_INDEX_TITLE

urlpatterns = [
    path('admin/', admin.site.urls),
    path('',RootApiView.as_view(),name='root-api'),
    path('api/v1/accounts/', include('accounts.urls')),
    path("api/v1/company/", include("company.urls")),
    path("api/v1/driver-management/", include("driver_management.urls")),
    path("api/v1/vehicle-management/", include("vehicle_management.urls")),
    path("api/v1/security-checks/", include("security_checks.urls")),
    path("api/v1/po/", include("sap_client.urls")),
    path("api/v1/raw-material-gatein/", include("raw_material_gatein.urls")),
    path("api/v1/weighment/", include("weighment.urls")),
    path("api/v1/quality-control/", include("quality_control.urls")),
    path("api/v1/gate-core/", include("gate_core.urls")),
    path("api/v1/daily-needs-gatein/", include("daily_needs_gatein.urls")),
    path("api/v1/maintenance-gatein/", include("maintenance_gatein.urls")),
    path("api/v1/construction-gatein/", include("construction_gatein.urls")),
    path("api/v1/person-gatein/", include("person_gatein.urls")),
    path("api/v1/grpo/", include("grpo.urls")),
    path("api/v1/notifications/", include("notifications.urls")),
    path("api/v1/production-execution/", include("production_execution.urls")),
    path("api/v1/sap/plan-dashboard/", include("sap_plan_dashboard.urls")),
    path("api/v1/dashboards/stock/", include("stock_dashboard.urls")),
    path("api/v1/dashboards/inventory-age/", include("inventory_age.urls")),
    path("api/v1/non-moving-rm/", include("non_moving_rm.urls")),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)