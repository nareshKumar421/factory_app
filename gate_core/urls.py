from django.urls import path
from .views import (
    RawMaterialGateEntryFullView,
    DailyNeedGateEntryFullView,
    MaintenanceGateEntryFullView,
    ConstructionGateEntryFullView,
    OutboundGateEntryFullView,
    UnitChoiceListView,
    GateAttachmentListCreateView,
)

urlpatterns = [
    # Unit Choice URLs
    path('unit-choices/', UnitChoiceListView.as_view(), name='unit_choice_list'),

    # Gate Attachment URLs
    path('gate-attachments/<int:gate_entry_id>/', GateAttachmentListCreateView.as_view(), name='gate_attachment_list_create'),

    # Gate Entry URLs
    path('raw-material-gate-entry/<int:gate_entry_id>/', RawMaterialGateEntryFullView.as_view(), name='raw_material_gate_entry_full_view'),
    path('daily-need-gate-entry/<int:gate_entry_id>/', DailyNeedGateEntryFullView.as_view(), name='daily_need_gate_entry_full_view'),
    path('maintenance-gate-entry/<int:gate_entry_id>/', MaintenanceGateEntryFullView.as_view(), name='maintenance_gate_entry_full_view'),
    path('construction-gate-entry/<int:gate_entry_id>/', ConstructionGateEntryFullView.as_view(), name='construction_gate_entry_full_view'),
    path('outbound-gate-entry/<int:gate_entry_id>/', OutboundGateEntryFullView.as_view(), name='outbound_gate_entry_full_view'),
]