"""Mounted by the lead at ``api/v1/cash-sap/`` (``config/urls.py``)."""

from django.urls import path

from . import views

urlpatterns = [
    path("options/", views.CashSapOptionsAPI.as_view(), name="cash-sap-options"),

    # The bunch's SAP page.
    path("bunches/", views.BunchSapStatusListAPI.as_view(), name="cash-sap-bunch-statuses"),
    path("bunches/<int:bunch_id>/", views.BunchSapPlanAPI.as_view(), name="cash-sap-bunch-plan"),
    path("bunches/<int:bunch_id>/post/", views.BunchSapPostAPI.as_view(), name="cash-sap-bunch-post"),
    path("bunches/<int:bunch_id>/send-next/", views.BunchSapSendNextAPI.as_view(), name="cash-sap-bunch-send-next"),
    path("bunches/<int:bunch_id>/stop/", views.BunchSapStopAPI.as_view(), name="cash-sap-bunch-stop"),
    path("bunches/<int:bunch_id>/recheck/", views.BunchSapRecheckAPI.as_view(), name="cash-sap-bunch-recheck"),
    path("bunches/<int:bunch_id>/accept-changes/", views.BunchSapAcceptChangesAPI.as_view(),
         name="cash-sap-bunch-accept-changes"),
    path("bunches/<int:bunch_id>/batches/<int:batch_id>/close-po/", views.BatchClosePOAPI.as_view(),
         name="cash-sap-batch-close-po"),
    path("bunches/<int:bunch_id>/vouchers/<int:entry_id>/", views.VoucherSapOverrideAPI.as_view(),
         name="cash-sap-voucher-override"),
    path("bunches/<int:bunch_id>/vouchers/<int:entry_id>/leave-out/", views.VoucherLeaveOutAPI.as_view(),
         name="cash-sap-voucher-leave-out"),
    path("bunches/<int:bunch_id>/grpos/<int:grpo_id>/", views.GrpoRedateAPI.as_view(), name="cash-sap-grpo-redate"),

    # One voucher, in or out of a bunch.
    path("vouchers/<int:entry_id>/info/", views.VoucherInfoAPI.as_view(), name="cash-sap-voucher-info"),
    path("vouchers/<int:entry_id>/bills/", views.VoucherBillAPI.as_view(), name="cash-sap-voucher-bills"),
    path("bills/<int:bill_id>/", views.VoucherBillDetailAPI.as_view(), name="cash-sap-bill-detail"),

    # Settings.
    path("routing/", views.BranchRoutingListAPI.as_view(), name="cash-sap-routing"),
    path("routing/<int:branch_id>/", views.BranchRoutingDetailAPI.as_view(), name="cash-sap-routing-detail"),
    path("gl-map/", views.GLItemMapListCreateAPI.as_view(), name="cash-sap-gl-map"),
    path("gl-map/<int:pk>/", views.GLItemMapDetailAPI.as_view(), name="cash-sap-gl-map-detail"),
    path("items/", views.SapItemSearchAPI.as_view(), name="cash-sap-items"),
]
