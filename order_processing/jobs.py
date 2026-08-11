"""Scheduled work, following this project's existing pattern.

There is no Celery here — ``maintenance/jobs.py`` uses APScheduler and cron-invoked
management commands, so this does the same rather than introducing a broker for
three periodic reads.

The order matters and is not arbitrary: syncing before processing means the engine
never decides against yesterday's orders, and planning materials last means BOMs
are exploded against requirements that were computed moments earlier rather than a
cycle ago.

    sync (15 min) -> process (30 min) -> plan materials (hourly)

Every step is idempotent, so a missed or doubled run is harmless.
"""
import logging

logger = logging.getLogger(__name__)


def sync_oms_orders_job():
    from .integrations.oms.reader import OmsUnavailable
    from .services.order_sync import sync_orders

    try:
        run = sync_orders(actor="scheduler")
    except OmsUnavailable as exc:
        # Logged, not raised: an OMS outage must not kill the scheduler thread and
        # take the other jobs with it.
        logger.warning("Scheduled OMS sync failed: %s", exc)
        return None
    logger.info("OMS sync: %s order(s), %s line(s)", run.orders_seen, run.lines_written)
    return run


def process_orders_job(limit=200):
    from .services.processing import process_pending

    tally = process_pending(limit=limit, actor="scheduler")
    logger.info("Processed orders: %s", tally)
    return tally


def plan_materials_job(bom_depth=1):
    from .services.material_planning import plan_all

    summary = plan_all(bom_depth=bom_depth)
    logger.info("Material planning: %s", summary)
    return summary


def register(scheduler):
    """Attach the jobs to an APScheduler instance.

    Called from wherever this project starts its scheduler, mirroring
    ``maintenance``. Kept as a function so importing this module has no side
    effects — a module that schedules work on import cannot be tested.
    """
    scheduler.add_job(sync_oms_orders_job, "interval", minutes=15,
                      id="op_sync_oms_orders", replace_existing=True,
                      max_instances=1, coalesce=True)
    scheduler.add_job(process_orders_job, "interval", minutes=30,
                      id="op_process_orders", replace_existing=True,
                      max_instances=1, coalesce=True)
    scheduler.add_job(plan_materials_job, "interval", hours=1,
                      id="op_plan_materials", replace_existing=True,
                      max_instances=1, coalesce=True)
    return scheduler
