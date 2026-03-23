# Utilities Mobile — Integration into Sampooran (Django)

This document outlines how to migrate the PHP-based Utilities Mobile app into the Sampooran Django project as new Django apps, reusing existing infrastructure (auth, notifications, company, gate core).

---

## Table of Contents

- [Integration Overview](#integration-overview)
- [What Already Exists in Sampooran](#what-already-exists-in-sampooran)
- [New Django Apps to Create](#new-django-apps-to-create)
- [App 1: `docking`](#app-1-docking)
- [App 2: `dynamic_forms`](#app-2-dynamic_forms)
- [App 3: `material_tracking`](#app-3-material_tracking)
- [App 4: `reporting`](#app-4-reporting)
- [Extend: `notifications` (WhatsApp)](#extend-notifications-whatsapp)
- [Extend: `sap_client` (Invoice Lookup)](#extend-sap_client-invoice-lookup)
- [URL Configuration](#url-configuration)
- [Permissions & Roles](#permissions--roles)
- [File Upload Handling](#file-upload-handling)
- [Scheduled Tasks (Management Commands)](#scheduled-tasks-management-commands)
- [Environment Variables](#environment-variables)
- [Migration Plan](#migration-plan)
- [Dependencies to Add](#dependencies-to-add)

---

## Integration Overview

The PHP Utilities Mobile app has 6 major feature areas. Here's how each maps into Sampooran:

| PHP Feature | Django Strategy | App |
|---|---|---|
| Authentication & sessions | **Reuse** existing `accounts` (JWT) | `accounts` (no changes) |
| Multi-company access | **Reuse** existing `company` app | `company` (no changes) |
| Dynamic form system | **New app** `dynamic_forms` | `dynamic_forms` |
| Approval workflow | Built into `dynamic_forms` | `dynamic_forms` |
| Docking & invoice processing | **New app** `docking` | `docking` |
| Material tracking (outward/returns/gate passes) | **New app** `material_tracking` | `material_tracking` |
| Reporting dashboards | **New app** `reporting` | `reporting` |
| WhatsApp notifications | **Extend** existing `notifications` | `notifications` |
| Firebase push notifications | **Reuse** existing `notifications` | `notifications` (no changes) |
| File uploads (image capture) | **Reuse** existing Django media config (15 MB limit) | No new app needed |
| SAP HANA queries | **Extend** existing `sap_client` with invoice lookup | `sap_client` |

### PHP → Django File Mapping

| PHP File(s) | Django Replacement |
|---|---|
| `check_login.php`, `validate_session.php`, `logout.php` | `accounts` app (already exists) |
| `list_forms.php`, `get_questions.php`, `submit_response.php` | `dynamic_forms` app |
| `approve_submission.php`, `utility_approval.php` | `dynamic_forms` approval views |
| `dock_invoice.php`, `dock_photo.php`, `empty_truck_in.php` | `docking` app |
| `gate_invoice2.php`, `gate_finalprint.php` | `material_tracking` gate pass views |
| `set_out_movement.php`, `confirm_received.php` | `material_tracking` movement views |
| `reporting.php`, `material_out_report.php`, `planningreport.php` | `reporting` app |
| `polling.php` – `polling11.php` | `notifications` WhatsApp + management commands |
| `config.php` | `config/settings.py` env variables |

---

## What Already Exists in Sampooran

These modules are **already built** and will be reused directly — no duplication needed:

### Authentication (`accounts`)
- Custom User model (email-based, `AUTH_USER_MODEL = "accounts.User"`)
- JWT tokens via Simple JWT (access: 1500 min, refresh: 7 days)
- Token rotation with blacklist
- Login, token refresh, password change, user list APIs
- Rate limiting: 50/hr anonymous, 500/hr authenticated

### Multi-Company (`company`)
- Company model with code-based identification (JIVO_OIL, JIVO_MART, JIVO_BEVERAGES)
- UserCompany model with role assignment (Admin, QC, Store, etc.)
- `Company-Code` header required for all requests
- `HasCompanyContext` permission class enforces company access

### Base Model (`gate_core.models.base`)
All new models must inherit from `BaseModel` which provides:
```python
class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="%(class)s_created")
    updated_by = models.ForeignKey(AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="%(class)s_updated")
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True
```

### Notifications (`notifications`)
- `UserDevice` model for FCM token management (WEB, ANDROID, iOS)
- `Notification` model with deep linking (`reference_type`, `reference_id`)
- `NotificationType` constants for categorized notifications
- `NotificationService` with methods:
  - `send_notification_to_user()` — single user, all devices
  - `send_notification_to_group()` — list of users
  - `send_bulk_notification()` — all company users, optionally by role
  - `send_notification_by_permission()` — users with specific permission
  - `send_notification_by_auth_group()` — users in Django auth group
- Already supports: mark as read, unread count, filtering by type/company

### File Uploads
- `MEDIA_URL = '/media/'`, `MEDIA_ROOT = os.path.join(BASE_DIR, 'media')`
- Max upload size: 15 MB (`DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`)
- `GateAttachment` model in `gate_core` as reference pattern
- Pillow installed for image processing

### SAP Integration (`sap_client`)
- HANA database queries (POs, vendors, warehouses)
- Service Layer integration for GRPO posting
- Per-company SAP database mapping

### Code Patterns (Must Follow)

| Pattern | Convention |
|---|---|
| Views | `APIView` (NOT ViewSet/ModelViewSet) |
| Permissions | `[IsAuthenticated, HasCompanyContext]` on all views |
| Serializers | Separate read serializer (`ModelSerializer`) + write serializer (`Serializer`) |
| URLs | Simple `path()` with explicit `.as_view()`, no DRF routers |
| Naming | `{Entity}ListCreateAPI`, `{Entity}DetailAPI`, `{Action}API` |
| Models | Inherit from `gate_core.models.base.BaseModel` |
| Company filter | `company=request.company` in all querysets |
| Audit fields | Set `created_by=request.user` on create, `updated_by=request.user` on update |

---

## New Django Apps to Create

### App 1: `docking`

Handles truck docking operations, invoice processing, and photo capture — replaces `dock_invoice.php`, `dock_photo.php`, `empty_truck_in.php`, `gate_invoice2.php`, `gate_finalprint.php`.

#### Directory Structure

```
docking/
├── __init__.py
├── apps.py
├── models.py
├── serializers.py
├── views.py
├── urls.py
├── admin.py
├── services.py          # Business logic (completion rules, SAP verification)
├── permissions.py       # App-specific permissions
└── migrations/
    └── __init__.py
```

#### Models

```python
# docking/models.py

from gate_core.models.base import BaseModel


class DockingEntryType(models.TextChoices):
    INWARD = 'INWARD', 'Inward'
    OUTWARD = 'OUTWARD', 'Outward'
    EMPTY_TRUCK = 'EMPTY_TRUCK', 'Empty Truck'


class DockingStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    DOCKED = 'DOCKED', 'Docked'
    INVOICE_VERIFIED = 'INVOICE_VERIFIED', 'Invoice Verified'
    COMPLETED = 'COMPLETED', 'Completed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class DockingPhotoType(models.TextChoices):
    VEHICLE = 'VEHICLE', 'Vehicle'
    INVOICE = 'INVOICE', 'Invoice'
    MATERIAL = 'MATERIAL', 'Material'
    SEAL = 'SEAL', 'Seal'
    OTHER = 'OTHER', 'Other'


class DockingEntry(BaseModel):
    """A truck docking event at the factory gate."""
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='docking_entries')
    entry_number = models.CharField(max_length=50)
    vehicle_number = models.CharField(max_length=20)
    driver_name = models.CharField(max_length=100, blank=True)
    driver_contact = models.CharField(max_length=15, blank=True)
    transporter_name = models.CharField(max_length=200, blank=True)
    entry_type = models.CharField(max_length=20, choices=DockingEntryType.choices)
    status = models.CharField(max_length=20, choices=DockingStatus.choices,
                              default=DockingStatus.DRAFT)
    dock_in_time = models.DateTimeField(null=True, blank=True)
    dock_out_time = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True)
    is_locked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('company', 'entry_number')
        ordering = ['-created_at']
        permissions = [
            ('can_view_docking', 'Can view docking entries'),
            ('can_manage_docking', 'Can create/edit docking entries'),
            ('can_verify_invoice', 'Can verify docking invoices against SAP'),
        ]

    def __str__(self):
        return f"{self.entry_number} - {self.vehicle_number}"


class DockingInvoice(BaseModel):
    """Invoice linked to a docking entry, verified against SAP."""
    docking_entry = models.ForeignKey(DockingEntry, on_delete=models.CASCADE,
                                      related_name='invoices')
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField()
    supplier_code = models.CharField(max_length=50, blank=True)
    supplier_name = models.CharField(max_length=200, blank=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True,
                                        help_text="SAP document entry ID")
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    is_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name='verified_docking_invoices')
    verified_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_number} ({self.docking_entry.entry_number})"


class DockingPhoto(BaseModel):
    """Photo captured during docking operations."""
    docking_entry = models.ForeignKey(DockingEntry, on_delete=models.CASCADE,
                                      related_name='photos')
    image = models.ImageField(upload_to='docking/photos/%Y/%m/%d/')
    photo_type = models.CharField(max_length=30, choices=DockingPhotoType.choices)
    caption = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-created_at']
```

#### Serializers

```python
# docking/serializers.py

# --- Read Serializers ---

class DockingInvoiceSerializer(serializers.ModelSerializer):
    verified_by_name = serializers.CharField(source='verified_by.full_name',
                                             read_only=True, default='')

    class Meta:
        model = DockingInvoice
        fields = ['id', 'invoice_number', 'invoice_date', 'supplier_code',
                  'supplier_name', 'sap_doc_entry', 'total_amount', 'is_verified',
                  'verified_by', 'verified_by_name', 'verified_at', 'remarks',
                  'created_at']
        read_only_fields = ['created_at', 'is_verified', 'verified_by', 'verified_at']


class DockingPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = DockingPhoto
        fields = ['id', 'image', 'photo_type', 'caption', 'created_at']
        read_only_fields = ['created_at']


class DockingEntryListSerializer(serializers.ModelSerializer):
    invoice_count = serializers.SerializerMethodField()
    photo_count = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.full_name',
                                            read_only=True, default='')

    class Meta:
        model = DockingEntry
        fields = ['id', 'entry_number', 'vehicle_number', 'driver_name',
                  'transporter_name', 'entry_type', 'status', 'dock_in_time',
                  'dock_out_time', 'is_locked', 'invoice_count', 'photo_count',
                  'created_by_name', 'created_at']

    def get_invoice_count(self, obj):
        return obj.invoices.count()

    def get_photo_count(self, obj):
        return obj.photos.count()


class DockingEntryDetailSerializer(serializers.ModelSerializer):
    invoices = DockingInvoiceSerializer(many=True, read_only=True)
    photos = DockingPhotoSerializer(many=True, read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name',
                                            read_only=True, default='')

    class Meta:
        model = DockingEntry
        fields = ['id', 'entry_number', 'vehicle_number', 'driver_name',
                  'driver_contact', 'transporter_name', 'entry_type', 'status',
                  'dock_in_time', 'dock_out_time', 'remarks', 'is_locked',
                  'invoices', 'photos', 'created_by_name', 'created_at',
                  'updated_at']


# --- Write Serializers ---

class DockingEntryCreateSerializer(serializers.Serializer):
    vehicle_number = serializers.CharField(max_length=20)
    driver_name = serializers.CharField(max_length=100, required=False, default='')
    driver_contact = serializers.CharField(max_length=15, required=False, default='')
    transporter_name = serializers.CharField(max_length=200, required=False, default='')
    entry_type = serializers.ChoiceField(choices=DockingEntryType.choices)
    remarks = serializers.CharField(required=False, default='')


class DockingInvoiceCreateSerializer(serializers.Serializer):
    invoice_number = serializers.CharField(max_length=50)
    invoice_date = serializers.DateField()
    supplier_code = serializers.CharField(max_length=50, required=False, default='')
    supplier_name = serializers.CharField(max_length=200, required=False, default='')
    total_amount = serializers.DecimalField(max_digits=15, decimal_places=2, default=0)
    remarks = serializers.CharField(required=False, default='')


class DockingPhotoCreateSerializer(serializers.Serializer):
    image = serializers.ImageField(required=False)
    image_base64 = serializers.CharField(required=False)
    photo_type = serializers.ChoiceField(choices=DockingPhotoType.choices)
    caption = serializers.CharField(max_length=200, required=False, default='')

    def validate(self, data):
        if not data.get('image') and not data.get('image_base64'):
            raise serializers.ValidationError("Either 'image' or 'image_base64' is required.")
        return data
```

#### Views

```python
# docking/views.py

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from company.permissions import HasCompanyContext


class DockingEntryListCreateAPI(APIView):
    """GET: list docking entries. POST: create new entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        entries = DockingEntry.objects.filter(
            company=request.company, is_active=True
        )
        # Filter by query params: status, entry_type, date_from, date_to
        status_filter = request.query_params.get('status')
        if status_filter:
            entries = entries.filter(status=status_filter)
        entry_type = request.query_params.get('entry_type')
        if entry_type:
            entries = entries.filter(entry_type=entry_type)
        date_from = request.query_params.get('date_from')
        if date_from:
            entries = entries.filter(created_at__date__gte=date_from)
        date_to = request.query_params.get('date_to')
        if date_to:
            entries = entries.filter(created_at__date__lte=date_to)

        serializer = DockingEntryListSerializer(entries, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = DockingEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        entry = DockingEntry.objects.create(
            company=request.company,
            entry_number=DockingService.generate_entry_number(request.company),
            created_by=request.user,
            **data,
        )
        return Response(
            DockingEntryDetailSerializer(entry).data,
            status=status.HTTP_201_CREATED,
        )


class DockingEntryDetailAPI(APIView):
    """GET: entry detail. PATCH: update entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        return Response(DockingEntryDetailSerializer(entry).data)

    def patch(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        if entry.is_locked:
            return Response({"error": "Entry is locked and cannot be modified."},
                            status=status.HTTP_400_BAD_REQUEST)
        # Update allowed fields...
        entry.updated_by = request.user
        entry.save()
        return Response(DockingEntryDetailSerializer(entry).data)


class DockInAPI(APIView):
    """POST: record dock-in time."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        if entry.is_locked:
            return Response({"error": "Entry is locked."}, status=status.HTTP_400_BAD_REQUEST)
        if entry.dock_in_time:
            return Response({"error": "Already docked in."}, status=status.HTTP_400_BAD_REQUEST)

        entry.dock_in_time = timezone.now()
        entry.status = DockingStatus.DOCKED
        entry.updated_by = request.user
        entry.save()
        return Response(DockingEntryDetailSerializer(entry).data)


class DockOutCompleteAPI(APIView):
    """POST: record dock-out and complete entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        if entry.is_locked:
            return Response({"error": "Entry is locked."}, status=status.HTTP_400_BAD_REQUEST)

        # Completion validation
        errors = DockingService.validate_completion(entry)
        if errors:
            return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

        entry.dock_out_time = timezone.now()
        entry.status = DockingStatus.COMPLETED
        entry.is_locked = True
        entry.updated_by = request.user
        entry.save()

        # Notify
        NotificationService.send_notification_to_user(
            user=entry.created_by,
            title="Docking Completed",
            body=f"Docking {entry.entry_number} completed for {entry.vehicle_number}.",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
            reference_type="docking_entry",
            reference_id=entry.id,
            company=entry.company,
            created_by=request.user,
        )

        return Response(DockingEntryDetailSerializer(entry).data)


class DockingInvoiceListCreateAPI(APIView):
    """GET: list invoices for entry. POST: add invoice."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        serializer = DockingInvoiceSerializer(entry.invoices.all(), many=True)
        return Response(serializer.data)

    def post(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        if entry.is_locked:
            return Response({"error": "Entry is locked."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = DockingInvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invoice = DockingInvoice.objects.create(
            docking_entry=entry,
            created_by=request.user,
            **serializer.validated_data,
        )
        return Response(DockingInvoiceSerializer(invoice).data,
                        status=status.HTTP_201_CREATED)


class DockingInvoiceVerifyAPI(APIView):
    """POST: verify invoice against SAP HANA."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id, invoice_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        invoice = get_object_or_404(DockingInvoice, id=invoice_id,
                                    docking_entry=entry)

        # Call SAP client to verify
        sap_data = SAPClient.get_invoice_by_number(
            company_code=request.company.code,
            invoice_number=invoice.invoice_number,
        )

        if not sap_data:
            return Response({"error": "Invoice not found in SAP."},
                            status=status.HTTP_404_NOT_FOUND)

        invoice.sap_doc_entry = sap_data.get('doc_entry')
        invoice.supplier_code = sap_data.get('supplier_code', invoice.supplier_code)
        invoice.supplier_name = sap_data.get('supplier_name', invoice.supplier_name)
        invoice.total_amount = sap_data.get('total_amount', invoice.total_amount)
        invoice.is_verified = True
        invoice.verified_by = request.user
        invoice.verified_at = timezone.now()
        invoice.updated_by = request.user
        invoice.save()

        # Update docking entry status if all invoices verified
        all_verified = entry.invoices.filter(is_verified=False).count() == 0
        if all_verified and entry.status == DockingStatus.DOCKED:
            entry.status = DockingStatus.INVOICE_VERIFIED
            entry.updated_by = request.user
            entry.save()

        return Response(DockingInvoiceSerializer(invoice).data)


class DockingPhotoListCreateAPI(APIView):
    """GET: list photos. POST: upload photo (multipart or base64)."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        return Response(DockingPhotoSerializer(entry.photos.all(), many=True).data)

    def post(self, request, entry_id):
        entry = get_object_or_404(DockingEntry, id=entry_id,
                                  company=request.company, is_active=True)
        if entry.is_locked:
            return Response({"error": "Entry is locked."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = DockingPhotoCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Handle base64 or multipart image
        image = data.get('image')
        if not image and data.get('image_base64'):
            image = decode_base64_image(data['image_base64'], filename_prefix='dock')

        photo = DockingPhoto.objects.create(
            docking_entry=entry,
            image=image,
            photo_type=data['photo_type'],
            caption=data.get('caption', ''),
            created_by=request.user,
        )
        return Response(DockingPhotoSerializer(photo).data,
                        status=status.HTTP_201_CREATED)


class SAPInvoiceLookupAPI(APIView):
    """GET: query SAP HANA for invoice data."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        invoice_number = request.query_params.get('invoice_number')
        if not invoice_number:
            return Response({"error": "invoice_number is required."},
                            status=status.HTTP_400_BAD_REQUEST)

        sap_data = SAPClient.get_invoice_by_number(
            company_code=request.company.code,
            invoice_number=invoice_number,
        )
        if not sap_data:
            return Response({"error": "Invoice not found in SAP."},
                            status=status.HTTP_404_NOT_FOUND)

        return Response(sap_data)
```

#### Services

```python
# docking/services.py

class DockingService:

    @staticmethod
    def generate_entry_number(company):
        """Generate unique entry number: DOCK-JIVO_OIL-20260323-001"""
        today = timezone.now().date()
        prefix = f"DOCK-{company.code}-{today.strftime('%Y%m%d')}"
        last = DockingEntry.objects.filter(
            company=company,
            entry_number__startswith=prefix,
        ).order_by('-entry_number').first()

        if last:
            seq = int(last.entry_number.split('-')[-1]) + 1
        else:
            seq = 1
        return f"{prefix}-{seq:03d}"

    @staticmethod
    def validate_completion(entry):
        """Check if docking entry can be completed. Returns list of errors."""
        errors = []
        if not entry.dock_in_time:
            errors.append("Dock-in time not recorded.")
        if entry.entry_type != DockingEntryType.EMPTY_TRUCK:
            if entry.invoices.count() == 0:
                errors.append("At least one invoice is required.")
            if entry.invoices.filter(is_verified=False).exists():
                errors.append("All invoices must be verified.")
        if entry.photos.count() == 0:
            errors.append("At least one photo is required.")
        return errors
```

#### URLs

```python
# docking/urls.py

from django.urls import path
from . import views

urlpatterns = [
    # Docking entries
    path('entries/', views.DockingEntryListCreateAPI.as_view(),
         name='docking-entry-list-create'),
    path('entries/<int:entry_id>/', views.DockingEntryDetailAPI.as_view(),
         name='docking-entry-detail'),
    path('entries/<int:entry_id>/dock-in/', views.DockInAPI.as_view(),
         name='docking-dock-in'),
    path('entries/<int:entry_id>/complete/', views.DockOutCompleteAPI.as_view(),
         name='docking-complete'),

    # Invoices
    path('entries/<int:entry_id>/invoices/', views.DockingInvoiceListCreateAPI.as_view(),
         name='docking-invoice-list-create'),
    path('entries/<int:entry_id>/invoices/<int:invoice_id>/verify/',
         views.DockingInvoiceVerifyAPI.as_view(), name='docking-invoice-verify'),

    # Photos
    path('entries/<int:entry_id>/photos/', views.DockingPhotoListCreateAPI.as_view(),
         name='docking-photo-list-create'),

    # SAP lookup
    path('sap/invoice-lookup/', views.SAPInvoiceLookupAPI.as_view(),
         name='docking-sap-invoice-lookup'),
]
```

#### Admin

```python
# docking/admin.py

class DockingInvoiceInline(admin.TabularInline):
    model = DockingInvoice
    extra = 0
    readonly_fields = ['verified_by', 'verified_at']

class DockingPhotoInline(admin.TabularInline):
    model = DockingPhoto
    extra = 0

@admin.register(DockingEntry)
class DockingEntryAdmin(admin.ModelAdmin):
    list_display = ['entry_number', 'vehicle_number', 'entry_type', 'status',
                    'company', 'dock_in_time', 'is_locked', 'created_at']
    list_filter = ['status', 'entry_type', 'company', 'is_locked']
    search_fields = ['entry_number', 'vehicle_number', 'driver_name']
    readonly_fields = ['created_by', 'updated_by', 'created_at', 'updated_at']
    inlines = [DockingInvoiceInline, DockingPhotoInline]
```

---

### App 2: `dynamic_forms`

Database-driven form builder with approval workflow — replaces `tbl_questions`, `tbl_responses`, `list_forms.php`, `get_questions.php`, `submit_response.php`, `approve_submission.php`.

#### Directory Structure

```
dynamic_forms/
├── __init__.py
├── apps.py
├── models.py
├── serializers.py
├── views.py
├── urls.py
├── admin.py
├── services.py          # Submission & approval logic, notification triggers
├── permissions.py
└── migrations/
    └── __init__.py
```

#### Models

```python
# dynamic_forms/models.py

from gate_core.models.base import BaseModel


class FieldType(models.TextChoices):
    TEXT = 'TEXT', 'Text'
    INTEGER = 'INTEGER', 'Integer'
    DECIMAL = 'DECIMAL', 'Decimal'
    DATE = 'DATE', 'Date'
    DATETIME = 'DATETIME', 'Date & Time'
    SELECT = 'SELECT', 'Dropdown Select'
    RADIO = 'RADIO', 'Radio Buttons'
    CHECKBOX = 'CHECKBOX', 'Checkbox'
    IMAGE = 'IMAGE', 'Image Capture'
    FILE = 'FILE', 'File Upload'
    TEXTAREA = 'TEXTAREA', 'Text Area'


class SubmissionStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    CLOSED = 'CLOSED', 'Closed'


class Form(BaseModel):
    """A configurable form template."""
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='dynamic_forms')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_published = models.BooleanField(default=False)
    requires_approval = models.BooleanField(default=True)
    approval_roles = models.ManyToManyField('company.UserRole', blank=True,
                                            help_text="Roles that can approve submissions")

    class Meta:
        unique_together = ('company', 'title')
        ordering = ['title']
        permissions = [
            ('can_manage_forms', 'Can create/edit form templates'),
            ('can_submit_forms', 'Can submit form responses'),
            ('can_approve_submissions', 'Can approve/reject submissions'),
        ]

    def __str__(self):
        return f"{self.title} ({self.company.code})"


class FormField(BaseModel):
    """A single field/question within a form."""
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='fields')
    label = models.CharField(max_length=300)
    field_type = models.CharField(max_length=20, choices=FieldType.choices)
    is_required = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    options = models.JSONField(default=list, blank=True,
                               help_text='Options for SELECT/RADIO/CHECKBOX: ["Option A", "Option B"]')
    validation_rules = models.JSONField(default=dict, blank=True,
                                        help_text='e.g. {"min": 0, "max": 100, "max_length": 500}')

    class Meta:
        ordering = ['order']
        unique_together = ('form', 'order')

    def __str__(self):
        return f"{self.form.title} → {self.label}"


class FormPermission(BaseModel):
    """Controls which users can access which forms."""
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='user_permissions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='form_permissions')
    can_submit = models.BooleanField(default=True)
    can_approve = models.BooleanField(default=False)

    class Meta:
        unique_together = ('form', 'user')


class FormSubmission(BaseModel):
    """A user's submission of a form."""
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='submissions')
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='form_submissions')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                     related_name='form_submissions')
    status = models.CharField(max_length=20, choices=SubmissionStatus.choices,
                              default=SubmissionStatus.PENDING)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name='approved_form_submissions')
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    is_closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"#{self.id} - {self.form.title} by {self.submitted_by.full_name}"


class FieldResponse(BaseModel):
    """Answer to a single form field within a submission."""
    submission = models.ForeignKey(FormSubmission, on_delete=models.CASCADE,
                                   related_name='responses')
    field = models.ForeignKey(FormField, on_delete=models.CASCADE,
                              related_name='responses')
    value = models.TextField(blank=True)
    image = models.ImageField(upload_to='forms/responses/%Y/%m/%d/', null=True, blank=True)
    file = models.FileField(upload_to='forms/files/%Y/%m/%d/', null=True, blank=True)

    class Meta:
        unique_together = ('submission', 'field')
```

#### Serializers

```python
# dynamic_forms/serializers.py

# --- Read Serializers ---

class FormFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = FormField
        fields = ['id', 'label', 'field_type', 'is_required', 'order',
                  'options', 'validation_rules']


class FormListSerializer(serializers.ModelSerializer):
    field_count = serializers.SerializerMethodField()
    submission_count = serializers.SerializerMethodField()

    class Meta:
        model = Form
        fields = ['id', 'title', 'description', 'is_published',
                  'requires_approval', 'field_count', 'submission_count',
                  'created_at']

    def get_field_count(self, obj):
        return obj.fields.count()

    def get_submission_count(self, obj):
        return obj.submissions.count()


class FormDetailSerializer(serializers.ModelSerializer):
    fields = FormFieldSerializer(many=True, read_only=True)

    class Meta:
        model = Form
        fields = ['id', 'title', 'description', 'is_published',
                  'requires_approval', 'fields', 'created_at', 'updated_at']


class FieldResponseSerializer(serializers.ModelSerializer):
    field_label = serializers.CharField(source='field.label', read_only=True)
    field_type = serializers.CharField(source='field.field_type', read_only=True)

    class Meta:
        model = FieldResponse
        fields = ['id', 'field', 'field_label', 'field_type', 'value',
                  'image', 'file']


class FormSubmissionListSerializer(serializers.ModelSerializer):
    form_title = serializers.CharField(source='form.title', read_only=True)
    submitted_by_name = serializers.CharField(source='submitted_by.full_name',
                                              read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.full_name',
                                             read_only=True, default='')

    class Meta:
        model = FormSubmission
        fields = ['id', 'form', 'form_title', 'submitted_by', 'submitted_by_name',
                  'status', 'approved_by_name', 'approved_at', 'is_closed',
                  'created_at']


class FormSubmissionDetailSerializer(serializers.ModelSerializer):
    form_title = serializers.CharField(source='form.title', read_only=True)
    submitted_by_name = serializers.CharField(source='submitted_by.full_name',
                                              read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.full_name',
                                             read_only=True, default='')
    responses = FieldResponseSerializer(many=True, read_only=True)

    class Meta:
        model = FormSubmission
        fields = ['id', 'form', 'form_title', 'submitted_by', 'submitted_by_name',
                  'status', 'approved_by', 'approved_by_name', 'approved_at',
                  'rejection_reason', 'is_closed', 'closed_at', 'responses',
                  'created_at', 'updated_at']


# --- Write Serializers ---

class FormCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, default='')
    requires_approval = serializers.BooleanField(default=True)


class FormFieldCreateSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=300)
    field_type = serializers.ChoiceField(choices=FieldType.choices)
    is_required = serializers.BooleanField(default=True)
    order = serializers.IntegerField(min_value=0)
    options = serializers.ListField(child=serializers.CharField(), required=False, default=[])
    validation_rules = serializers.DictField(required=False, default={})


class FormSubmitSerializer(serializers.Serializer):
    """Accepts a list of field responses for submission."""
    responses = serializers.ListField(child=serializers.DictField())
    # Each dict: {"field_id": 1, "value": "answer", "image_base64": "...", "file": <UploadedFile>}

    def validate_responses(self, responses):
        for r in responses:
            if 'field_id' not in r:
                raise serializers.ValidationError("Each response must have 'field_id'.")
        return responses


class ApprovalSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField(required=False, default='')
```

#### Views

```python
# dynamic_forms/views.py

class FormListCreateAPI(APIView):
    """GET: list accessible forms. POST: create form (admin)."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        if request.user.has_perm('dynamic_forms.can_manage_forms'):
            forms = Form.objects.filter(company=request.company, is_active=True)
        else:
            permitted_ids = FormPermission.objects.filter(
                user=request.user, can_submit=True
            ).values_list('form_id', flat=True)
            forms = Form.objects.filter(
                id__in=permitted_ids, company=request.company,
                is_published=True, is_active=True,
            )
        return Response(FormListSerializer(forms, many=True).data)

    def post(self, request):
        if not request.user.has_perm('dynamic_forms.can_manage_forms'):
            return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        serializer = FormCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        form = Form.objects.create(
            company=request.company,
            created_by=request.user,
            **serializer.validated_data,
        )
        return Response(FormDetailSerializer(form).data, status=status.HTTP_201_CREATED)


class FormDetailAPI(APIView):
    """GET: form with fields. PATCH: update form."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, form_id):
        form = get_object_or_404(Form, id=form_id, company=request.company, is_active=True)
        return Response(FormDetailSerializer(form).data)

    def patch(self, request, form_id):
        if not request.user.has_perm('dynamic_forms.can_manage_forms'):
            return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        form = get_object_or_404(Form, id=form_id, company=request.company, is_active=True)
        # Update allowed fields...
        form.updated_by = request.user
        form.save()
        return Response(FormDetailSerializer(form).data)


class FormSubmitAPI(APIView):
    """POST: submit form with responses."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, form_id):
        form = get_object_or_404(Form, id=form_id, company=request.company,
                                 is_published=True, is_active=True)
        serializer = FormSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        submission = FormSubmissionService.create_submission(
            form=form,
            company=request.company,
            user=request.user,
            responses_data=serializer.validated_data['responses'],
        )

        return Response(FormSubmissionDetailSerializer(submission).data,
                        status=status.HTTP_201_CREATED)


class SubmissionApproveAPI(APIView):
    """POST: approve a submission."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, submission_id):
        submission = get_object_or_404(FormSubmission, id=submission_id,
                                       company=request.company)
        if submission.status != SubmissionStatus.PENDING:
            return Response({"error": "Only pending submissions can be approved."},
                            status=status.HTTP_400_BAD_REQUEST)

        FormSubmissionService.approve(submission, approved_by=request.user)
        return Response(FormSubmissionDetailSerializer(submission).data)


class SubmissionRejectAPI(APIView):
    """POST: reject a submission."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, submission_id):
        submission = get_object_or_404(FormSubmission, id=submission_id,
                                       company=request.company)
        if submission.status != SubmissionStatus.PENDING:
            return Response({"error": "Only pending submissions can be rejected."},
                            status=status.HTTP_400_BAD_REQUEST)

        serializer = ApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        FormSubmissionService.reject(
            submission,
            rejected_by=request.user,
            reason=serializer.validated_data.get('rejection_reason', ''),
        )
        return Response(FormSubmissionDetailSerializer(submission).data)


class PendingApprovalsAPI(APIView):
    """GET: list pending approvals for current user."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        # User can approve forms where they have can_approve permission
        approvable_form_ids = FormPermission.objects.filter(
            user=request.user, can_approve=True
        ).values_list('form_id', flat=True)

        submissions = FormSubmission.objects.filter(
            company=request.company,
            form_id__in=approvable_form_ids,
            status=SubmissionStatus.PENDING,
        )
        return Response(FormSubmissionListSerializer(submissions, many=True).data)
```

#### Services

```python
# dynamic_forms/services.py

class FormSubmissionService:

    @staticmethod
    @transaction.atomic
    def create_submission(form, company, user, responses_data):
        """Create submission with all field responses."""
        submission = FormSubmission.objects.create(
            form=form,
            company=company,
            submitted_by=user,
            created_by=user,
            status=SubmissionStatus.PENDING,
        )

        for resp in responses_data:
            field = FormField.objects.get(id=resp['field_id'], form=form)

            # Validate required fields
            if field.is_required and not resp.get('value') and not resp.get('image_base64'):
                raise ValidationError(f"Field '{field.label}' is required.")

            # Handle image
            image = None
            if resp.get('image_base64'):
                image = decode_base64_image(resp['image_base64'], filename_prefix='form')

            FieldResponse.objects.create(
                submission=submission,
                field=field,
                value=resp.get('value', ''),
                image=image,
                created_by=user,
            )

        # Notify approvers
        approvers = FormPermission.objects.filter(
            form=form, can_approve=True
        ).select_related('user')

        for perm in approvers:
            NotificationService.send_notification_to_user(
                user=perm.user,
                title=f"New Submission: {form.title}",
                body=f"{user.full_name} submitted '{form.title}'. Please review.",
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                reference_type="form_submission",
                reference_id=submission.id,
                company=company,
                created_by=user,
            )

        # WhatsApp notification
        WhatsAppService.send_pending_approval_alert(submission)

        return submission

    @staticmethod
    @transaction.atomic
    def approve(submission, approved_by):
        submission.status = SubmissionStatus.APPROVED
        submission.approved_by = approved_by
        submission.approved_at = timezone.now()
        submission.updated_by = approved_by
        submission.save()

        # Notify submitter
        NotificationService.send_notification_to_user(
            user=submission.submitted_by,
            title="Submission Approved",
            body=f"Your submission for '{submission.form.title}' was approved by {approved_by.full_name}.",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
            reference_type="form_submission",
            reference_id=submission.id,
            company=submission.company,
            created_by=approved_by,
        )
        WhatsAppService.send_approval_notification(submission, 'APPROVED')

    @staticmethod
    @transaction.atomic
    def reject(submission, rejected_by, reason=''):
        submission.status = SubmissionStatus.REJECTED
        submission.approved_by = rejected_by
        submission.approved_at = timezone.now()
        submission.rejection_reason = reason
        submission.updated_by = rejected_by
        submission.save()

        NotificationService.send_notification_to_user(
            user=submission.submitted_by,
            title="Submission Rejected",
            body=f"Your submission for '{submission.form.title}' was rejected. Reason: {reason}",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
            reference_type="form_submission",
            reference_id=submission.id,
            company=submission.company,
            created_by=rejected_by,
        )
        WhatsAppService.send_approval_notification(submission, 'REJECTED')
```

#### URLs

```python
# dynamic_forms/urls.py

urlpatterns = [
    # Forms
    path('forms/', views.FormListCreateAPI.as_view(), name='df-form-list-create'),
    path('forms/<int:form_id>/', views.FormDetailAPI.as_view(), name='df-form-detail'),
    path('forms/<int:form_id>/fields/', views.FormFieldListCreateAPI.as_view(),
         name='df-field-list-create'),
    path('forms/<int:form_id>/fields/<int:field_id>/', views.FormFieldDetailAPI.as_view(),
         name='df-field-detail'),
    path('forms/<int:form_id>/submit/', views.FormSubmitAPI.as_view(), name='df-form-submit'),

    # Submissions
    path('submissions/', views.SubmissionListAPI.as_view(), name='df-submission-list'),
    path('submissions/<int:submission_id>/', views.SubmissionDetailAPI.as_view(),
         name='df-submission-detail'),
    path('submissions/<int:submission_id>/approve/', views.SubmissionApproveAPI.as_view(),
         name='df-submission-approve'),
    path('submissions/<int:submission_id>/reject/', views.SubmissionRejectAPI.as_view(),
         name='df-submission-reject'),
    path('submissions/<int:submission_id>/close/', views.SubmissionCloseAPI.as_view(),
         name='df-submission-close'),

    # Dashboard
    path('pending-approvals/', views.PendingApprovalsAPI.as_view(),
         name='df-pending-approvals'),
]
```

---

### App 3: `material_tracking`

Tracks outward material movement, returns, and gate passes — replaces `set_out_movement.php`, `confirm_received.php`, `gate_invoice2.php`, `gate_finalprint.php`.

#### Directory Structure

```
material_tracking/
├── __init__.py
├── apps.py
├── models.py
├── serializers.py
├── views.py
├── urls.py
├── admin.py
├── services.py          # Number generation, overdue detection, auto-close
├── permissions.py
├── management/
│   └── commands/
│       └── check_overdue_movements.py
└── migrations/
    └── __init__.py
```

#### Models

```python
# material_tracking/models.py

from gate_core.models.base import BaseModel


class MovementType(models.TextChoices):
    OUTWARD = 'OUTWARD', 'Outward'
    RETURN = 'RETURN', 'Return'
    TRANSFER = 'TRANSFER', 'Inter-location Transfer'


class MovementStatus(models.TextChoices):
    DISPATCHED = 'DISPATCHED', 'Dispatched'
    IN_TRANSIT = 'IN_TRANSIT', 'In Transit'
    RECEIVED = 'RECEIVED', 'Received'
    PARTIALLY_RECEIVED = 'PARTIALLY_RECEIVED', 'Partially Received'
    OVERDUE = 'OVERDUE', 'Overdue'
    CLOSED = 'CLOSED', 'Closed'


class GatePassType(models.TextChoices):
    RETURNABLE = 'RETURNABLE', 'Returnable'
    NON_RETURNABLE = 'NON_RETURNABLE', 'Non-Returnable'


class GatePassStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending Approval'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    CLOSED = 'CLOSED', 'Closed'


class MaterialMovement(BaseModel):
    """Tracks outward material dispatches and returns."""
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='material_movements')
    movement_number = models.CharField(max_length=50)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    status = models.CharField(max_length=20, choices=MovementStatus.choices,
                              default=MovementStatus.DISPATCHED)

    # Source & Destination
    from_location = models.CharField(max_length=200)
    to_location = models.CharField(max_length=200)

    # Vehicle info
    vehicle_number = models.CharField(max_length=20, blank=True)
    driver_name = models.CharField(max_length=100, blank=True)
    driver_contact = models.CharField(max_length=15, blank=True)

    # Dispatch
    dispatched_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                      related_name='dispatched_movements')
    dispatched_at = models.DateTimeField(auto_now_add=True)

    # Return tracking
    expected_return_date = models.DateField(null=True, blank=True)
    received_back = models.BooleanField(default=False)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name='received_movements')
    received_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)
    is_locked = models.BooleanField(default=False)

    class Meta:
        unique_together = ('company', 'movement_number')
        ordering = ['-created_at']
        permissions = [
            ('can_view_movements', 'Can view material movements'),
            ('can_manage_movements', 'Can create/dispatch movements'),
            ('can_receive_materials', 'Can confirm material receipt'),
            ('can_manage_gate_passes', 'Can create gate passes'),
            ('can_approve_gate_passes', 'Can approve gate passes'),
        ]

    def __str__(self):
        return f"{self.movement_number} ({self.movement_type})"


class MovementItem(BaseModel):
    """Individual item within a material movement."""
    movement = models.ForeignKey(MaterialMovement, on_delete=models.CASCADE,
                                 related_name='items')
    material_name = models.CharField(max_length=200)
    material_code = models.CharField(max_length=50, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    uom = models.CharField(max_length=20, help_text="Unit of measure")
    received_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    remarks = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return f"{self.material_name} x {self.quantity} {self.uom}"


class GatePass(BaseModel):
    """Gate pass document for material entry/exit."""
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='gate_passes')
    gate_pass_number = models.CharField(max_length=50)
    pass_type = models.CharField(max_length=20, choices=GatePassType.choices)
    movement = models.ForeignKey(MaterialMovement, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='gate_passes')
    status = models.CharField(max_length=20, choices=GatePassStatus.choices,
                              default=GatePassStatus.DRAFT)

    # Party details
    party_name = models.CharField(max_length=200)
    party_contact = models.CharField(max_length=15, blank=True)
    vehicle_number = models.CharField(max_length=20, blank=True)

    # Approval
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name='approved_gate_passes')
    approved_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)

    class Meta:
        unique_together = ('company', 'gate_pass_number')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.gate_pass_number} ({self.pass_type})"


class GatePassItem(BaseModel):
    """Individual item on a gate pass."""
    gate_pass = models.ForeignKey(GatePass, on_delete=models.CASCADE,
                                  related_name='items')
    material_name = models.CharField(max_length=200)
    material_code = models.CharField(max_length=50, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    uom = models.CharField(max_length=20)
    value = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    remarks = models.CharField(max_length=300, blank=True)
```

#### Management Command — Overdue Detection

```python
# material_tracking/management/commands/check_overdue_movements.py

class Command(BaseCommand):
    help = 'Mark overdue movements and send WhatsApp reminders.'

    def handle(self, *args, **options):
        today = timezone.now().date()
        overdue = MaterialMovement.objects.filter(
            status__in=[MovementStatus.DISPATCHED, MovementStatus.IN_TRANSIT,
                        MovementStatus.PARTIALLY_RECEIVED],
            expected_return_date__lt=today,
        ).exclude(status=MovementStatus.OVERDUE)

        count = 0
        for movement in overdue:
            movement.status = MovementStatus.OVERDUE
            movement.save(update_fields=['status', 'updated_at'])

            # Push notification
            NotificationService.send_notification_to_user(
                user=movement.dispatched_by,
                title="Material Movement Overdue",
                body=f"{movement.movement_number} is overdue. Expected return: {movement.expected_return_date}.",
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                reference_type="material_movement",
                reference_id=movement.id,
                company=movement.company,
            )

            # WhatsApp reminder
            WhatsAppService.send_overdue_reminder(movement)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Marked {count} movements as overdue."))
```

**Cron schedule (add to server crontab):**
```bash
# Run daily at 8:00 AM
0 8 * * * cd /path/to/project && python manage.py check_overdue_movements
```

#### URLs

```python
# material_tracking/urls.py

urlpatterns = [
    # Movements
    path('movements/', views.MovementListCreateAPI.as_view(),
         name='mt-movement-list-create'),
    path('movements/<int:movement_id>/', views.MovementDetailAPI.as_view(),
         name='mt-movement-detail'),
    path('movements/<int:movement_id>/dispatch/', views.MovementDispatchAPI.as_view(),
         name='mt-movement-dispatch'),
    path('movements/<int:movement_id>/receive/', views.MovementReceiveAPI.as_view(),
         name='mt-movement-receive'),
    path('movements/<int:movement_id>/close/', views.MovementCloseAPI.as_view(),
         name='mt-movement-close'),
    path('movements/overdue/', views.OverdueMovementsAPI.as_view(),
         name='mt-overdue-movements'),

    # Gate Passes
    path('gate-passes/', views.GatePassListCreateAPI.as_view(),
         name='mt-gate-pass-list-create'),
    path('gate-passes/<int:pass_id>/', views.GatePassDetailAPI.as_view(),
         name='mt-gate-pass-detail'),
    path('gate-passes/<int:pass_id>/approve/', views.GatePassApproveAPI.as_view(),
         name='mt-gate-pass-approve'),
    path('gate-passes/<int:pass_id>/reject/', views.GatePassRejectAPI.as_view(),
         name='mt-gate-pass-reject'),
    path('gate-passes/<int:pass_id>/print/', views.GatePassPrintAPI.as_view(),
         name='mt-gate-pass-print'),
]
```

---

### App 4: `reporting`

SQL-based analytics and reporting dashboards — replaces `reporting.php`, `reporting_android.php`, `material_out_report.php`, `planningreport.php`.

#### Directory Structure

```
reporting/
├── __init__.py
├── apps.py
├── models.py
├── serializers.py
├── views.py
├── urls.py
├── admin.py
├── services.py          # Query execution (read-only conn), export generation
├── permissions.py
├── management/
│   └── commands/
│       └── run_scheduled_reports.py
└── migrations/
    └── __init__.py
```

#### Models

```python
# reporting/models.py

from gate_core.models.base import BaseModel


class ReportCategory(models.TextChoices):
    PRODUCTION = 'PRODUCTION', 'Production'
    MATERIAL = 'MATERIAL', 'Material'
    GATE = 'GATE', 'Gate Operations'
    QUALITY = 'QUALITY', 'Quality Control'
    DOCKING = 'DOCKING', 'Docking'
    GENERAL = 'GENERAL', 'General'


class ScheduleType(models.TextChoices):
    DAILY = 'DAILY', 'Daily'
    WEEKLY = 'WEEKLY', 'Weekly'
    HALF_MONTHLY = 'HALF_MONTHLY', 'Half-Monthly (1st & 16th)'
    MONTHLY = 'MONTHLY', 'Monthly'


class ReportDefinition(BaseModel):
    """Admin-defined report with a parameterized query."""
    company = models.ForeignKey('company.Company', on_delete=models.CASCADE,
                                related_name='report_definitions')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=50, choices=ReportCategory.choices)
    query_template = models.TextField(
        help_text="SQL query with named params: %(start_date)s, %(end_date)s. "
                  "MUST use parameterized queries — never string format user input.")
    parameters = models.JSONField(
        default=list,
        help_text='[{"name": "start_date", "type": "date", "required": true, "label": "Start Date"}]')
    columns = models.JSONField(
        default=list,
        help_text='[{"key": "col_name", "label": "Display Name", "type": "string"}]')
    is_published = models.BooleanField(default=False)
    allowed_roles = models.ManyToManyField('company.UserRole', blank=True)

    class Meta:
        unique_together = ('company', 'name')
        ordering = ['category', 'name']
        permissions = [
            ('can_view_reports', 'Can view and execute reports'),
            ('can_manage_reports', 'Can create/edit report definitions'),
            ('can_manage_schedules', 'Can manage scheduled reports'),
        ]

    def __str__(self):
        return f"{self.name} ({self.category})"


class ScheduledReport(BaseModel):
    """Scheduled report that runs automatically and sends results via email."""
    report = models.ForeignKey(ReportDefinition, on_delete=models.CASCADE,
                               related_name='schedules')
    schedule_type = models.CharField(max_length=20, choices=ScheduleType.choices)
    parameters = models.JSONField(default=dict,
                                  help_text="Fixed parameter values for this schedule")
    recipients = models.ManyToManyField(settings.AUTH_USER_MODEL,
                                        related_name='subscribed_reports')
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.report.name} ({self.schedule_type})"


class ReportExecutionLog(BaseModel):
    """Audit log for report executions."""
    report = models.ForeignKey(ReportDefinition, on_delete=models.CASCADE,
                               related_name='execution_logs')
    executed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL)
    parameters_used = models.JSONField(default=dict)
    row_count = models.IntegerField(default=0)
    execution_time_ms = models.IntegerField(default=0)
    is_scheduled = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
```

#### Services

```python
# reporting/services.py

class ReportService:

    @staticmethod
    def execute_report(report, parameters, executed_by=None, is_scheduled=False):
        """Execute report query with parameters on a READ-ONLY connection.

        SECURITY: Uses Django's cursor with parameterized queries.
        The query_template uses %(name)s placeholders which psycopg2
        handles safely as parameterized queries.
        """
        import time
        start = time.monotonic()

        with connections['default'].cursor() as cursor:
            cursor.execute(report.query_template, parameters)
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchall()

        elapsed_ms = int((time.monotonic() - start) * 1000)

        # Audit log
        ReportExecutionLog.objects.create(
            report=report,
            executed_by=executed_by,
            parameters_used=parameters,
            row_count=len(rows),
            execution_time_ms=elapsed_ms,
            is_scheduled=is_scheduled,
            created_by=executed_by,
        )

        return {
            'columns': [{'key': c, 'label': c} for c in columns],
            'rows': [dict(zip(columns, row)) for row in rows],
            'total_count': len(rows),
            'execution_time_ms': elapsed_ms,
        }

    @staticmethod
    def export_to_excel(report_data, report_name):
        """Generate Excel file from report data. Returns BytesIO."""
        from openpyxl import Workbook
        from io import BytesIO

        wb = Workbook()
        ws = wb.active
        ws.title = report_name[:31]

        # Header row
        columns = report_data['columns']
        for col_idx, col in enumerate(columns, 1):
            ws.cell(row=1, column=col_idx, value=col['label'])

        # Data rows
        for row_idx, row in enumerate(report_data['rows'], 2):
            for col_idx, col in enumerate(columns, 1):
                ws.cell(row=row_idx, column=col_idx, value=row.get(col['key'], ''))

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return output

    @staticmethod
    def calculate_next_run(schedule_type, from_date=None):
        """Calculate the next run date for a scheduled report."""
        now = from_date or timezone.now()
        if schedule_type == ScheduleType.DAILY:
            return now + timedelta(days=1)
        elif schedule_type == ScheduleType.WEEKLY:
            return now + timedelta(weeks=1)
        elif schedule_type == ScheduleType.HALF_MONTHLY:
            if now.day < 16:
                return now.replace(day=16)
            else:
                next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
                return next_month
        elif schedule_type == ScheduleType.MONTHLY:
            next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
            return next_month.replace(day=min(now.day, 28))
```

#### Management Command — Scheduled Reports

```python
# reporting/management/commands/run_scheduled_reports.py

class Command(BaseCommand):
    help = 'Execute due scheduled reports and email results.'

    def handle(self, *args, **options):
        now = timezone.now()
        due = ScheduledReport.objects.filter(is_active=True, next_run_at__lte=now)

        for schedule in due:
            try:
                result = ReportService.execute_report(
                    report=schedule.report,
                    parameters=schedule.parameters,
                    is_scheduled=True,
                )

                # Generate Excel
                excel = ReportService.export_to_excel(result, schedule.report.name)

                # Email to recipients
                recipients = list(schedule.recipients.values_list('email', flat=True))
                if recipients:
                    email = EmailMessage(
                        subject=f"Scheduled Report: {schedule.report.name}",
                        body=f"Report: {schedule.report.name}\n"
                             f"Rows: {result['total_count']}\n"
                             f"Generated: {now.strftime('%Y-%m-%d %H:%M')}",
                        to=recipients,
                    )
                    email.attach(
                        f"{schedule.report.name}_{now.strftime('%Y%m%d')}.xlsx",
                        excel.read(),
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    )
                    email.send()

                # Update schedule
                schedule.last_run_at = now
                schedule.next_run_at = ReportService.calculate_next_run(schedule.schedule_type, now)
                schedule.save()

                self.stdout.write(self.style.SUCCESS(
                    f"Executed: {schedule.report.name} → {len(recipients)} recipients"))

            except Exception as e:
                self.stderr.write(self.style.ERROR(
                    f"Failed: {schedule.report.name} — {str(e)}"))
```

**Cron schedule:**
```bash
# Run every hour to check for due reports
0 * * * * cd /path/to/project && python manage.py run_scheduled_reports
```

#### URLs

```python
# reporting/urls.py

urlpatterns = [
    path('reports/', views.ReportListAPI.as_view(), name='rpt-report-list'),
    path('reports/<int:report_id>/', views.ReportDetailAPI.as_view(), name='rpt-report-detail'),
    path('reports/<int:report_id>/execute/', views.ReportExecuteAPI.as_view(),
         name='rpt-report-execute'),
    path('reports/<int:report_id>/export/', views.ReportExportAPI.as_view(),
         name='rpt-report-export'),

    path('schedules/', views.ScheduleListCreateAPI.as_view(), name='rpt-schedule-list-create'),
    path('schedules/<int:schedule_id>/', views.ScheduleDetailAPI.as_view(),
         name='rpt-schedule-detail'),
]
```

---

## Extend: `notifications` (WhatsApp)

Add WhatsApp notification support via AiSensy API to the existing `notifications` app.

#### New Model

```python
# notifications/models.py (add to existing)

class WhatsAppTemplate(BaseModel):
    """WhatsApp message template registered with AiSensy."""
    template_name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=300, blank=True)
    parameter_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.template_name


class WhatsAppLog(BaseModel):
    """Audit log for WhatsApp messages sent."""
    template = models.ForeignKey(WhatsAppTemplate, on_delete=models.SET_NULL,
                                 null=True, blank=True)
    phone_number = models.CharField(max_length=15)
    parameters = models.JSONField(default=list)
    status = models.CharField(max_length=20, default='SENT')
    error_message = models.TextField(blank=True)
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
```

#### New Service

```python
# notifications/services/whatsapp_service.py

import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Send WhatsApp messages via AiSensy API.

    Follows the same pattern as NotificationService — static/classmethod based.
    All messages are logged to WhatsAppLog for audit.
    """

    @staticmethod
    def _format_phone(phone_number):
        """Format phone to +91XXXXXXXXXX for Indian numbers."""
        phone = phone_number.strip().replace(' ', '').replace('-', '')
        if phone.startswith('+'):
            return phone
        if phone.startswith('91') and len(phone) == 12:
            return f"+{phone}"
        if len(phone) == 10:
            return f"+91{phone}"
        return f"+91{phone}"

    @classmethod
    def send_message(cls, phone_number, template_name, parameters=None,
                     reference_type='', reference_id=None):
        """Send a WhatsApp template message via AiSensy API."""
        api_url = getattr(settings, 'AISENSY_API_URL', '')
        api_key = getattr(settings, 'AISENSY_API_KEY', '')

        if not api_url or not api_key:
            logger.warning("WhatsApp not configured — AISENSY_API_URL or AISENSY_API_KEY missing.")
            return None

        formatted_phone = cls._format_phone(phone_number)
        params = parameters or []

        payload = {
            "apiKey": api_key,
            "campaignName": template_name,
            "destination": formatted_phone,
            "userName": "Sampooran",
            "templateParams": params,
        }

        log_entry = WhatsAppLog.objects.create(
            phone_number=formatted_phone,
            parameters=params,
            reference_type=reference_type,
            reference_id=reference_id,
            status='SENDING',
        )

        try:
            template = WhatsAppTemplate.objects.filter(
                template_name=template_name, is_active=True).first()
            if template:
                log_entry.template = template

            response = requests.post(api_url, json=payload, timeout=10)
            response.raise_for_status()

            log_entry.status = 'SENT'
            log_entry.save()
            logger.info(f"WhatsApp sent to {formatted_phone} template={template_name}")
            return response.json()

        except requests.RequestException as e:
            log_entry.status = 'FAILED'
            log_entry.error_message = str(e)
            log_entry.save()
            logger.error(f"WhatsApp failed to {formatted_phone}: {e}")
            return None

    @classmethod
    def send_approval_notification(cls, submission, status):
        """Send approval/rejection WhatsApp alert for form submission."""
        user = submission.submitted_by
        phone = getattr(user, 'phone_number', None) or getattr(user, 'driver_contact', '')
        if not phone:
            return None

        template = 'material_approve2'
        params = [
            user.full_name,
            submission.form.title,
            f"#{submission.id}",
            status,
        ]
        return cls.send_message(
            phone_number=phone,
            template_name=template,
            parameters=params,
            reference_type='form_submission',
            reference_id=submission.id,
        )

    @classmethod
    def send_material_dispatch_alert(cls, movement):
        """Send material dispatch WhatsApp alert."""
        user = movement.dispatched_by
        phone = getattr(user, 'phone_number', None) or ''
        if not phone:
            return None

        items_summary = ", ".join(
            f"{item.quantity} {item.uom} {item.material_name}"
            for item in movement.items.all()[:3]
        )

        template = 'material_out'
        params = [
            movement.movement_number,
            items_summary,
            movement.to_location,
        ]
        return cls.send_message(
            phone_number=phone,
            template_name=template,
            parameters=params,
            reference_type='material_movement',
            reference_id=movement.id,
        )

    @classmethod
    def send_overdue_reminder(cls, movement):
        """Send overdue material movement WhatsApp reminder."""
        user = movement.dispatched_by
        phone = getattr(user, 'phone_number', None) or ''
        if not phone:
            return None

        template = 'material_out'
        params = [
            movement.movement_number,
            f"OVERDUE since {movement.expected_return_date}",
            movement.to_location,
        ]
        return cls.send_message(
            phone_number=phone,
            template_name=template,
            parameters=params,
            reference_type='material_movement',
            reference_id=movement.id,
        )

    @classmethod
    def send_pending_approval_alert(cls, submission):
        """Send WhatsApp to approvers when new submission is pending."""
        approvers = FormPermission.objects.filter(
            form=submission.form, can_approve=True
        ).select_related('user')

        for perm in approvers:
            phone = getattr(perm.user, 'phone_number', None) or ''
            if phone:
                cls.send_message(
                    phone_number=phone,
                    template_name='material_approve2',
                    parameters=[
                        perm.user.full_name,
                        submission.form.title,
                        f"#{submission.id}",
                        "PENDING REVIEW",
                    ],
                    reference_type='form_submission',
                    reference_id=submission.id,
                )
```

#### WhatsApp Polling Management Command

```python
# notifications/management/commands/send_whatsapp_reminders.py

class Command(BaseCommand):
    help = 'Send WhatsApp reminders for pending approvals and overdue items.'

    def handle(self, *args, **options):
        now = timezone.now()
        threshold = now - timedelta(hours=4)

        # 1. Pending form approvals older than 4 hours
        pending = FormSubmission.objects.filter(
            status=SubmissionStatus.PENDING,
            created_at__lt=threshold,
        )
        for sub in pending:
            WhatsAppService.send_pending_approval_alert(sub)

        # 2. Overdue material movements
        overdue = MaterialMovement.objects.filter(status=MovementStatus.OVERDUE)
        for mov in overdue:
            WhatsAppService.send_overdue_reminder(mov)

        self.stdout.write(self.style.SUCCESS(
            f"Reminders sent: {pending.count()} approvals, {overdue.count()} overdue movements"))
```

**Cron schedule:**
```bash
# Run every 2 hours during work hours (8 AM - 8 PM)
0 8,10,12,14,16,18,20 * * * cd /path/to/project && python manage.py send_whatsapp_reminders
```

---

## Extend: `sap_client` (Invoice Lookup)

Add a new method to the existing SAP client for docking invoice verification.

```python
# sap_client/services.py (add to existing)

def get_invoice_by_number(company_code, invoice_number):
    """Query SAP HANA for invoice details — used by docking module.

    Returns dict with: doc_entry, supplier_code, supplier_name, total_amount, items
    Returns None if not found.
    """
    company_db = get_company_db(company_code)  # existing helper
    conn = get_hana_connection()               # existing helper

    query = """
        SELECT T0."DocEntry", T0."CardCode", T0."CardName", T0."DocTotal",
               T0."DocDate", T0."NumAtCard"
        FROM "{db}"."OPCH" T0
        WHERE T0."NumAtCard" = ?
        ORDER BY T0."DocDate" DESC
        LIMIT 1
    """.format(db=company_db)

    cursor = conn.cursor()
    cursor.execute(query, (invoice_number,))
    row = cursor.fetchone()

    if not row:
        return None

    return {
        'doc_entry': row[0],
        'supplier_code': row[1],
        'supplier_name': row[2],
        'total_amount': float(row[3]),
        'doc_date': str(row[4]),
        'invoice_number': row[5],
    }
```

---

## URL Configuration

Add to `config/urls.py`:

```python
urlpatterns = [
    # ... existing URLs ...

    # New apps (Utilities Mobile integration)
    path('api/v1/docking/', include('docking.urls')),
    path('api/v1/dynamic-forms/', include('dynamic_forms.urls')),
    path('api/v1/material-tracking/', include('material_tracking.urls')),
    path('api/v1/reporting/', include('reporting.urls')),
]
```

Add to `config/settings.py` INSTALLED_APPS:

```python
INSTALLED_APPS = [
    # ... existing apps ...

    # Utilities Mobile integration
    'docking',
    'dynamic_forms',
    'material_tracking',
    'reporting',
]
```

Add email config to `config/settings.py`:

```python
# Email (for scheduled reports)
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = True
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='sampooran@jivowellness.com')

# WhatsApp (AiSensy)
AISENSY_API_URL = config('AISENSY_API_URL', default='')
AISENSY_API_KEY = config('AISENSY_API_KEY', default='')
```

---

## Permissions & Roles

### New Permissions

| App | Permission | Description |
|---|---|---|
| `docking` | `can_view_docking` | View docking entries |
| `docking` | `can_manage_docking` | Create/edit docking entries |
| `docking` | `can_verify_invoice` | Verify docking invoices against SAP |
| `dynamic_forms` | `can_manage_forms` | Create/edit form templates |
| `dynamic_forms` | `can_submit_forms` | Submit form responses |
| `dynamic_forms` | `can_approve_submissions` | Approve/reject submissions |
| `material_tracking` | `can_view_movements` | View material movements |
| `material_tracking` | `can_manage_movements` | Create/dispatch movements |
| `material_tracking` | `can_receive_materials` | Confirm material receipt |
| `material_tracking` | `can_manage_gate_passes` | Create gate passes |
| `material_tracking` | `can_approve_gate_passes` | Approve gate passes |
| `reporting` | `can_view_reports` | View and execute reports |
| `reporting` | `can_manage_reports` | Create/edit report definitions |
| `reporting` | `can_manage_schedules` | Manage scheduled reports |

### New Groups

| Group | Permissions |
|---|---|
| `docking_operator` | `can_view_docking`, `can_manage_docking` |
| `docking_verifier` | `can_view_docking`, `can_verify_invoice` |
| `form_admin` | `can_manage_forms`, `can_submit_forms`, `can_approve_submissions` |
| `form_user` | `can_submit_forms` |
| `material_operator` | `can_view_movements`, `can_manage_movements`, `can_manage_gate_passes` |
| `material_receiver` | `can_view_movements`, `can_receive_materials` |
| `gate_pass_approver` | `can_view_movements`, `can_approve_gate_passes` |
| `report_viewer` | `can_view_reports` |
| `report_admin` | `can_view_reports`, `can_manage_reports`, `can_manage_schedules` |

### Setup Management Command

Extend the existing `setup_production_groups` command or create a new one:

```python
# management/commands/setup_utility_groups.py

GROUPS = {
    'docking_operator': ['can_view_docking', 'can_manage_docking'],
    'docking_verifier': ['can_view_docking', 'can_verify_invoice'],
    'form_admin': ['can_manage_forms', 'can_submit_forms', 'can_approve_submissions'],
    'form_user': ['can_submit_forms'],
    'material_operator': ['can_view_movements', 'can_manage_movements', 'can_manage_gate_passes'],
    'material_receiver': ['can_view_movements', 'can_receive_materials'],
    'gate_pass_approver': ['can_view_movements', 'can_approve_gate_passes'],
    'report_viewer': ['can_view_reports'],
    'report_admin': ['can_view_reports', 'can_manage_reports', 'can_manage_schedules'],
}
```

---

## File Upload Handling

Reuse existing Django media configuration (15 MB limit already set).

### Upload Path Convention

```
media/
├── docking/
│   └── photos/YYYY/MM/DD/          # Docking photos
├── forms/
│   ├── responses/YYYY/MM/DD/       # Form image responses
│   └── files/YYYY/MM/DD/           # Form file uploads
└── gate_core/                       # Already exists
    └── attachments/YYYY/MM/DD/
```

### Base64 Image Utility

For mobile camera uploads matching the PHP app's behavior:

```python
# core/utils/image_utils.py

import base64
from uuid import uuid4
from django.core.files.base import ContentFile


def decode_base64_image(data, filename_prefix='img'):
    """Convert base64 data URI string to a Django ContentFile.

    Accepts: "data:image/jpeg;base64,/9j/4AAQ..."
    Returns: ContentFile ready to assign to ImageField
    """
    if ';base64,' not in data:
        raise ValueError("Invalid base64 image format. Expected 'data:<mime>;base64,<data>'.")

    header, imgstr = data.split(';base64,')
    ext = header.split('/')[-1]
    if ext not in ('jpeg', 'jpg', 'png', 'gif', 'webp'):
        raise ValueError(f"Unsupported image type: {ext}")

    decoded = base64.b64decode(imgstr)

    # Enforce 15 MB limit
    if len(decoded) > 15 * 1024 * 1024:
        raise ValueError("Image exceeds 15 MB limit.")

    return ContentFile(decoded, name=f'{filename_prefix}_{uuid4().hex[:8]}.{ext}')
```

---

## Scheduled Tasks (Management Commands)

Since the project does **not** use Celery, all scheduled tasks are implemented as Django management commands run via cron:

| Command | Schedule | Purpose |
|---|---|---|
| `check_overdue_movements` | Daily at 8:00 AM | Mark overdue movements, send notifications |
| `run_scheduled_reports` | Hourly | Execute due reports, email results |
| `send_whatsapp_reminders` | Every 2 hrs (8 AM–8 PM) | Remind approvers and overdue receivers |

### Crontab Setup

```bash
# Utilities Mobile — Sampooran scheduled tasks
0 8 * * * cd /path/to/project && python manage.py check_overdue_movements >> /var/log/sampooran/overdue.log 2>&1
0 * * * * cd /path/to/project && python manage.py run_scheduled_reports >> /var/log/sampooran/reports.log 2>&1
0 8,10,12,14,16,18,20 * * * cd /path/to/project && python manage.py send_whatsapp_reminders >> /var/log/sampooran/whatsapp.log 2>&1
```

---

## Environment Variables

Add these to `.env` for the new modules:

```env
# Email (for scheduled reports)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=sampooran@jivowellness.com
EMAIL_HOST_PASSWORD=your-app-password
DEFAULT_FROM_EMAIL=sampooran@jivowellness.com

# WhatsApp (AiSensy)
AISENSY_API_URL=https://backend.aisensy.com/campaign/t1/api/v2
AISENSY_API_KEY=your-api-key
```

---

## Migration Plan

### Phase 1: Foundation (Week 1–2)
1. Create all 4 app directories with `django-admin startapp`
2. Add `BaseModel` import, define all models
3. Run `makemigrations` / `migrate`
4. Register models in Django admin
5. Create `setup_utility_groups` management command
6. Add `decode_base64_image` utility
7. Add WhatsApp models + service to `notifications`
8. Add new environment variables to settings
9. Add new apps to `INSTALLED_APPS` and `config/urls.py`

### Phase 2: Docking & Material Tracking (Week 3–4)
1. Build serializers (read + write) for `docking`
2. Build APIView views for `docking`
3. Build serializers and views for `material_tracking`
4. Add `get_invoice_by_number()` to `sap_client`
5. Implement `check_overdue_movements` management command
6. Write unit tests for both apps

### Phase 3: Dynamic Forms & Approvals (Week 5–6)
1. Build form builder API (forms, fields, permissions)
2. Build submission and approval workflow
3. Integrate WhatsApp + FCM notifications on approval/rejection
4. Implement `send_whatsapp_reminders` command
5. Write unit tests

### Phase 4: Reporting (Week 7)
1. Build report definition and execution API
2. Implement Excel export via `openpyxl`
3. Implement `run_scheduled_reports` management command
4. Add email config to settings
5. Write unit tests

### Phase 5: Data Migration & Cutover (Week 8)
1. Write Django management commands to migrate data from PHP PostgreSQL `utilities` DB:
   - `tbl_questions` → `Form` + `FormField`
   - `tbl_responses` → `FormSubmission` + `FieldResponse`
   - `tbl_permissions` → `FormPermission`
   - `tbl_login` → map to existing `accounts.User` by email/employee_code
2. Parallel run: both PHP and Django active
3. Validate data integrity
4. Set up cron jobs on production server
5. Switch over and decommission PHP endpoints

---

## Dependencies to Add

Add to `requirement.txt`:

```
requests>=2.31.0          # For AiSensy WhatsApp API calls
openpyxl>=3.1.0           # For Excel report export
```

> **Note:** No Celery dependency needed — scheduled tasks use management commands + cron, matching the project's existing pattern.

---

## Summary

| What | Action | App |
|---|---|---|
| Auth, JWT, Users | Reuse as-is | `accounts` |
| Multi-company | Reuse as-is | `company` |
| Base model (audit fields) | Reuse as-is | `gate_core` |
| Push notifications (FCM) | Reuse as-is | `notifications` |
| WhatsApp notifications | Extend with `WhatsAppService` + `WhatsAppLog` | `notifications` |
| SAP queries | Extend with `get_invoice_by_number()` | `sap_client` |
| File uploads | Reuse + add `decode_base64_image` utility | `core/utils/` |
| Docking & invoicing | **New app** — 3 models, 8 endpoints | `docking` |
| Dynamic forms + approvals | **New app** — 6 models, 13 endpoints | `dynamic_forms` |
| Material outward/returns/gate passes | **New app** — 4 models, 12 endpoints | `material_tracking` |
| Reporting dashboards | **New app** — 3 models, 6 endpoints | `reporting` |
| Scheduled tasks | 3 management commands + cron | Multiple apps |

### Total New Models: 16
### Total New API Endpoints: 39
### Total New Management Commands: 4
