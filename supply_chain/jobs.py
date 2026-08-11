"""Scheduled supply-chain work.

Registered in ``run_scheduler`` alongside the work-permit and returnable jobs, so
there is one scheduler process to start and one place to look when a job has not
fired.
"""
import logging

from django.conf import settings

from .services.live_trail import PRODUCTION_COMPANY
from .services.live_trail_autopilot import run_live_trail_autopilot

logger = logging.getLogger(__name__)


def run_live_trail_digest():
    """Read the trail and tell each department what it owns.

    Wrapped so a bad morning in SAP — an unreachable HANA box, a schema that was
    renamed — is a logged failure and a retry tomorrow, not a dead scheduler
    that also stops work-permit expiry.
    """
    company_code = getattr(settings, "LIVE_TRAIL_COMPANY_CODE", PRODUCTION_COMPANY)
    try:
        results = run_live_trail_autopilot(company_code)
    except Exception:  # noqa: BLE001 — the scheduler must survive it.
        logger.exception("Live trail autopilot failed for %s", company_code)
        return

    sent = [r for r in results if r.get("sent")]
    logger.info(
        "Live trail autopilot: %s department(s) notified, %s quiet",
        len(sent), len(results) - len(sent),
    )
