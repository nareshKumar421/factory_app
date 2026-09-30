from django.urls import path

from . import views

urlpatterns = [
    path('warehouses/', views.WarehousesAPI.as_view(), name='stock-audit-warehouses'),
    path('audits/', views.AuditListCreateAPI.as_view(), name='stock-audit-list'),
    path('audits/<int:audit_id>/', views.AuditDetailAPI.as_view(), name='stock-audit-detail'),
    path('audits/<int:audit_id>/lines/', views.AuditLinesAPI.as_view(), name='stock-audit-lines'),
    path('audits/<int:audit_id>/lines/<int:line_id>/counts/', views.LineCountsAPI.as_view(),
         name='stock-audit-line-counts'),
    path('audits/<int:audit_id>/counts/<int:count_id>/void/', views.CountVoidAPI.as_view(),
         name='stock-audit-count-void'),
    path('audits/<int:audit_id>/items/', views.AuditItemsAPI.as_view(), name='stock-audit-items'),
    path('audits/<int:audit_id>/refresh/', views.AuditRefreshAPI.as_view(),
         name='stock-audit-refresh'),
    path('audits/<int:audit_id>/complete/', views.AuditCompleteAPI.as_view(),
         name='stock-audit-complete'),
    path('audits/<int:audit_id>/approve/', views.AuditApproveAPI.as_view(),
         name='stock-audit-approve'),
    path('audits/<int:audit_id>/reject/', views.AuditRejectAPI.as_view(),
         name='stock-audit-reject'),
    path('audits/<int:audit_id>/sap-posting/', views.AuditPostToSapAPI.as_view(),
         name='stock-audit-sap-posting'),
    path('audits/<int:audit_id>/export/', views.AuditExportAPI.as_view(),
         name='stock-audit-export'),
]
