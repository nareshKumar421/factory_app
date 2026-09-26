"""The 7 pm read, as a scheduled job (registered in ``run_scheduler``).

At 7 pm: check today's plan against what the plant did, then read everything
and make the plan for the next working day. It tries again at 7:20 and 7:40
if SAP did not answer, and does nothing on those runs once a read has stood.
"""

import logging
from datetime import datetime, time

from django.db import close_old_connections
from django.utils import timezone

from company.models import Company

from . import services
from .inputs import InputsUnavailable, next_working_day
from .models import PlanningSheet, TomorrowPlan

logger = logging.getLogger(__name__)

READ_HOUR, READ_MINUTES = 19, "0,20,40"


def nightly_read():
    close_old_connections()
    now = timezone.localtime()
    since = timezone.make_aware(datetime.combine(now.date(), time(18, 55)))
    for company in Company.objects.filter(id__in=PlanningSheet.objects.values("company_id")).distinct():
        for_date = next_working_day(now.date())
        if TomorrowPlan.objects.filter(company=company, for_date=for_date, read_at__gte=since).exists():
            continue
        try:
            services.run_check(company, day=now.date(), now=now)
        except Exception:
            logger.exception("Tomorrow's run: the 7 pm check failed for %s", company.code)
        try:
            services.build(company, for_date=for_date, now=now)
            logger.info("Tomorrow's run: %s planned for %s", company.code, for_date)
        except InputsUnavailable as e:
            logger.error("Tomorrow's run: %s not read (%s); trying again at the next slot", company.code, e)
