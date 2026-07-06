from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings

User = settings.AUTH_USER_MODEL


# -------------------------------------------------
# Person Type (Visitor / Labour)
# -------------------------------------------------
class PersonType(models.Model):
    name = models.CharField(max_length=50, unique=True)  # Visitor / Labour
    is_active = models.BooleanField(default=True)

    class Meta:
        permissions = [
            ("can_manage_person_type", "Can manage person type"),
        ]

    def __str__(self):
        return self.name


# -------------------------------------------------
# Gate Master
# -------------------------------------------------
class Gate(models.Model):
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=150, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        permissions = [
            ("can_manage_gate", "Can manage gate"),
        ]

    def __str__(self):
        return self.name


# -------------------------------------------------
# Contractor (for labour)
# -------------------------------------------------
class Contractor(models.Model):
    contractor_name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=100, blank=True, null=True)
    mobile = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    contract_valid_till = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        pass

    def __str__(self):
        return self.contractor_name


# -------------------------------------------------
# Visitor Master
# -------------------------------------------------
class Visitor(models.Model):
    name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20, blank=True, null=True)
    company_name = models.CharField(max_length=150, blank=True, null=True)

    id_proof_type = models.CharField(max_length=50, blank=True, null=True)
    id_proof_no = models.CharField(max_length=100, blank=True, null=True)

    photo = models.ImageField(upload_to="visitor_photos/", blank=True, null=True)

    blacklisted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        pass

    def __str__(self):
        return f"{self.name} ({self.company_name})"


# -------------------------------------------------
# Labour Master
# -------------------------------------------------
class Labour(models.Model):
    name = models.CharField(max_length=150)

    contractor = models.ForeignKey(
        Contractor,
        on_delete=models.CASCADE,
        related_name="labours"
    )

    mobile = models.CharField(max_length=20, blank=True, null=True)
    id_proof_no = models.CharField(max_length=100, blank=True, null=True)

    photo = models.ImageField(upload_to="labour_photos/", blank=True, null=True)

    skill_type = models.CharField(max_length=100, blank=True, null=True)

    permit_valid_till = models.DateField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        pass

    def __str__(self):
        return f"{self.name} - {self.contractor.contractor_name}"


# -------------------------------------------------
# Entry Log (🔥 CORE TABLE)
# -------------------------------------------------
class EntryLog(models.Model):

    STATUS_CHOICES = (
        ("IN", "Inside"),
        ("OUT", "Exited"),
        ("CANCELLED", "Cancelled"),
    )

    person_type = models.ForeignKey(PersonType, on_delete=models.PROTECT)

    visitor = models.ForeignKey(
        Visitor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    labour = models.ForeignKey(
        Labour,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    # Snapshot fields (audit safety)
    name_snapshot = models.CharField(max_length=150)
    photo_snapshot = models.ImageField(upload_to="entry_snapshots/", blank=True, null=True)

    # Movement
    gate_in = models.ForeignKey(
        Gate,
        on_delete=models.PROTECT,
        related_name="entries_in"
    )

    gate_out = models.ForeignKey(
        Gate,
        on_delete=models.PROTECT,
        related_name="entries_out",
        null=True,
        blank=True
    )

    entry_time = models.DateTimeField(auto_now_add=True)
    actual_entry_time = models.DateTimeField(null=True, blank=True)
    exit_time = models.DateTimeField(null=True, blank=True)

    # Business
    purpose = models.CharField(max_length=255, blank=True, null=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_entries"
    )

    vehicle_no = models.CharField(max_length=50, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="IN")

    # Audit
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_entries"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["entry_time"]),
            models.Index(fields=["person_type"]),
            models.Index(
                fields=["status", "entry_time"],
                name="entrylog_status_time_idx",
            ),
            models.Index(
                fields=["labour", "status"],
                name="entrylog_labour_status_idx",
            ),
            models.Index(
                fields=["visitor", "status"],
                name="entrylog_visitor_status_idx",
            ),
        ]
        permissions = [
            ("can_cancel_entry", "Can cancel person gate entry"),
            ("can_exit_entry", "Can mark person gate exit"),
            ("can_search_entry", "Can search person gate entries"),
            ("can_view_dashboard", "Can view person gate dashboard"),
        ]

    def __str__(self):
        return f"{self.name_snapshot} - {self.status}"
