"""
stock_dashboard/models.py

Custom permissions for the Stock Dashboard and the StockAlertLog
model that prevents duplicate notifications.
"""

from django.db import models


class StockDashboardPermission(models.Model):
    """
    Sentinel model that holds custom permissions for the Stock Dashboard.
    No database rows are ever written to this table.
    """

    class Meta:
        managed = False  # No DB table created
        default_permissions = ()  # Don't generate add/view/change/delete
        permissions = [
            ("can_view_stock_dashboard", "Can view Stock Dashboard"),
        ]


class StockAlertLog(models.Model):
    """
    Tracks notifications sent for low/critical stock items to prevent
    duplicate alerts within the cooldown window.

    One record per (company_code, item_code, warehouse) combination.
    If cooldown_until > now(), the alert is suppressed.
    """

    company_code = models.CharField(max_length=50)
    item_code = models.CharField(max_length=50)
    warehouse = models.CharField(max_length=20)
    stock_status = models.CharField(
        max_length=10,
        choices=[("low", "Low"), ("critical", "Critical")],
    )
    on_hand = models.FloatField()
    min_stock = models.FloatField()
    notified_at = models.DateTimeField(auto_now=True)
    cooldown_until = models.DateTimeField(
        help_text="Do not re-send this alert until after this timestamp"
    )

    class Meta:
        unique_together = ("company_code", "item_code", "warehouse")
        indexes = [
            models.Index(fields=["cooldown_until"]),
        ]

    def __str__(self):
        return f"{self.item_code} @ {self.warehouse} ({self.stock_status})"
