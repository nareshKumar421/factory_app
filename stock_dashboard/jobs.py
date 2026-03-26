"""
stock_dashboard/jobs.py

Background job that checks SAP HANA for low/critical stock levels
and sends notifications to users with the can_view_stock_dashboard permission.

Uses StockAlertLog to prevent duplicate notifications within a cooldown window.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from company.models import Company
from notifications.models import NotificationType
from notifications.services import NotificationService
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import StockAlertLog
from .services import StockDashboardService

logger = logging.getLogger(__name__)

# Default cooldown: 60 minutes (configurable via STOCK_ALERT_COOLDOWN_MINUTES in .env)
DEFAULT_COOLDOWN_MINUTES = 60


def get_cooldown_minutes() -> int:
    return getattr(settings, "STOCK_ALERT_COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES)


def send_stock_alerts():
    """
    Main job entry point. Called by the APScheduler.

    For each active company:
      1. Queries SAP for items where OnHand < MinStock
      2. Checks StockAlertLog for cooldown
      3. Sends notifications via NotificationService
      4. Creates/updates StockAlertLog records
    """
    cooldown_minutes = get_cooldown_minutes()
    now = timezone.now()

    logger.info(f"[StockAlert] Starting stock alert check (cooldown={cooldown_minutes}min)")

    companies = Company.objects.filter(is_active=True)
    total_alerts = 0

    for company in companies:
        try:
            alerts_sent = _process_company(company, cooldown_minutes, now)
            total_alerts += alerts_sent
        except (SAPConnectionError, SAPDataError) as e:
            logger.warning(f"[StockAlert] SAP error for {company.code}: {e}")
        except Exception as e:
            logger.error(f"[StockAlert] Unexpected error for {company.code}: {e}", exc_info=True)

    logger.info(f"[StockAlert] Completed. {total_alerts} alerts sent across {companies.count()} companies.")


def _process_company(company: Company, cooldown_minutes: int, now) -> int:
    """Process stock alerts for a single company. Returns count of alerts sent."""
    service = StockDashboardService(company_code=company.code)

    # Get all items (no filters — we want everything where MinStock != 0)
    result = service.get_stock_levels({})
    rows = result["data"]

    # Only low and critical items need alerts
    alert_rows = [r for r in rows if r["stock_status"] in ("low", "critical")]

    if not alert_rows:
        return 0

    alerts_sent = 0
    cooldown_delta = timedelta(minutes=cooldown_minutes)

    for row in alert_rows:
        item_code = row["item_code"]
        warehouse = row["warehouse"]
        stock_status = row["stock_status"]

        # Check cooldown
        existing = StockAlertLog.objects.filter(
            company_code=company.code,
            item_code=item_code,
            warehouse=warehouse,
            cooldown_until__gt=now,
        ).first()

        if existing:
            # Still in cooldown — but if status worsened (low → critical), re-alert
            if not (stock_status == "critical" and existing.stock_status == "low"):
                continue

        # Build notification content
        severity = "Critical" if stock_status == "critical" else "Low"
        title = f"{severity} Stock Alert: {row['item_name']}"
        body = (
            f"{row['item_name']} ({item_code}) in warehouse {warehouse}: "
            f"{row['on_hand']:,.0f} on hand vs {row['min_stock']:,.0f} minimum "
            f"({row['uom']})"
        )

        try:
            click_url = f"/dashboards/stock-levels?search={item_code}"

            count = NotificationService.send_notification_by_permission(
                permission_codename="can_view_stock_dashboard",
                title=title,
                body=body,
                notification_type=NotificationType.STOCK_ALERT,
                click_action_url=click_url,
                company=company,
                extra_data={
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "stock_status": stock_status,
                    "on_hand": row["on_hand"],
                    "min_stock": row["min_stock"],
                },
            )

            # Create or update the alert log
            StockAlertLog.objects.update_or_create(
                company_code=company.code,
                item_code=item_code,
                warehouse=warehouse,
                defaults={
                    "stock_status": stock_status,
                    "on_hand": row["on_hand"],
                    "min_stock": row["min_stock"],
                    "cooldown_until": now + cooldown_delta,
                },
            )

            alerts_sent += 1
            logger.info(
                f"[StockAlert] Sent {severity} alert for {item_code}@{warehouse} "
                f"to {count} users ({company.code})"
            )

        except Exception as e:
            logger.error(
                f"[StockAlert] Failed to send notification for "
                f"{item_code}@{warehouse}: {e}",
                exc_info=True,
            )

    return alerts_sent
