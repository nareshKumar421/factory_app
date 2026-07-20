from django.db import models
from django.conf import settings

User = settings.AUTH_USER_MODEL


# -------------------------------------------------
# Employee Master (attendance config)
# -------------------------------------------------
class Employee(models.Model):
    """
    Simple employee record used only by the attendance fallback module.
    Not linked to anything except the shared accounts.Department master.
    """
    employee_code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="attendance_employees",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.employee_code})"


# -------------------------------------------------
# Attendance Record (manual gate entry)
# -------------------------------------------------
class AttendanceRecord(models.Model):
    """
    A single manual attendance mark, used as a fallback when the punching
    machine is unavailable. Each mark records whether the employee was coming
    IN or going OUT; an employee may be marked any number of times a day.
    """

    class Direction(models.TextChoices):
        IN = "IN", "In"
        OUT = "OUT", "Out"

    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    direction = models.CharField(
        max_length=3,
        choices=Direction.choices,
        default=Direction.IN,
    )
    date = models.DateField()
    time = models.TimeField()
    # Proof that the employee was indeed present at the marked time.
    photo = models.ImageField(upload_to="attendance_photos/")

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-time"]
        permissions = [
            ("can_view_attendance_dashboard", "Can view attendance dashboard"),
        ]

    def __str__(self):
        return f"{self.employee} - {self.date} {self.time} ({self.direction})"
