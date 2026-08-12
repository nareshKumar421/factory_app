"""Marketplace Gate Pass — the physical trip that takes a sheet's parcels off site.

The marketplace counterpart of the sales-dispatch gate-out, and deliberately the
same ladder: pick the vehicle, print the pass, weigh, then mark it out. Where a
rule already exists on the sales side it is mirrored rather than reinvented, so
a gate person moving between the two screens meets the same behaviour:

  * vehicle / transporter / driver are frozen onto the pass as text as well as
    FKs -- the printed pass must not change when a master record is renamed
    (``gate_core.SalesDispatchGateOut`` keeps its own copy for the same reason);
  * gross AND tare are required before anything leaves, and tare may not exceed
    gross (see ``sales_dispatch_dispatch.get_dispatch_weight_error``);
  * the gatepass number comes from a per-company, per-financial-year sequence.

What is NOT mirrored: the SAP-invoice steps (docking documents, delivery-note
posting, PGI). Marketplace parcels are already confirmed and gate-approved order
by order before they reach here; this document only answers "which vehicle took
them, what did it weigh, and when did it leave".
"""
import secrets

from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceGatePass,
    MarketplaceGatePassStatus,
    MarketplaceGateStatus,
    OrderImportBatch,
)
from .errors import MarketplaceError
from .gate_service import _batch_q, _parcels

# Statuses a pass can still be worked on from.
OPEN_STATUSES = (
    MarketplaceGatePassStatus.DRAFT,
    MarketplaceGatePassStatus.WEIGHED,
    MarketplaceGatePassStatus.GATEPASS_PRINTED,
)


def _get_pass(company, gate_pass_id, *, for_update=False):
    """Fetch one trip, optionally locked for a write.

    The locking read deliberately does NOT select_related: vehicle, transporter
    and driver are nullable, so joining them emits LEFT OUTER JOINs and
    PostgreSQL refuses — "FOR UPDATE cannot be applied to the nullable side of an
    outer join". SQLite ignores the lock entirely, so this only ever failed in
    production. The joins are an optimisation for the read path and nothing on
    the write path needs them, so they are simply not asked for when locking.
    """
    qs = MarketplaceGatePass.objects.filter(company=company, id=gate_pass_id, is_active=True)
    if for_update:
        gate_pass = qs.select_for_update().first()
    else:
        gate_pass = qs.select_related(
            "import_batch", "vehicle", "transporter", "driver").first()
    if gate_pass is None:
        raise MarketplaceError("Gate pass not found.", code="NOT_FOUND", status_code=404)
    return gate_pass


def eligible_dispatches(company, channel, batch_id):
    """Parcels a trip may carry: confirmed, approved at the gate, not yet gone.

    A parcel already stamped with a gate pass has physically left on another
    vehicle; loading it onto a second trip would double-count the stock that has
    gone out.
    """
    return (
        MarketplaceDispatch.objects
        .filter(
            company=company,
            channel=channel,
            status=MarketplaceDispatchStatus.CONFIRMED,
            gate_status=MarketplaceGateStatus.APPROVED,
            gate_pass__isnull=True,
        )
        .filter(_batch_q(batch_id))
        .select_related("order")
        .prefetch_related("scans")
    )


def _load_of(dispatches):
    """Orders and parcels on a set of dispatches.

    Orders are counted distinct: a re-manifested order ships as two parcels under
    two dispatch rows and is still one order.
    """
    return {
        "order_count": len({d.order_id for d in dispatches}),
        "parcel_count": sum(_parcels(d) for d in dispatches),
    }


@transaction.atomic
def create_gate_pass(
    company, channel, batch_id, *, user,
    vehicle=None, transporter=None, driver=None, remarks="",
):
    """Open a trip for a sheet, freezing who is driving it.

    The snapshot is taken now rather than at print time so the pass shows the
    vehicle it was raised against even if the master is edited in between.
    """
    batch = OrderImportBatch.objects.filter(id=batch_id, company=company).first()
    if batch is None:
        raise MarketplaceError("Sheet not found.", code="NOT_FOUND", status_code=404)

    load = _load_of(list(eligible_dispatches(company, channel, batch_id)))
    if not load["parcel_count"]:
        raise MarketplaceError(
            "No gate-approved parcels are waiting on this sheet. Approve them at "
            "the gate first.",
            code="NOTHING_TO_DISPATCH",
        )

    gate_pass = MarketplaceGatePass(
        company=company, channel=channel, import_batch=batch,
        status=MarketplaceGatePassStatus.DRAFT,
        remarks=remarks or "",
        created_by=user, updated_by=user,
    )
    _apply_transport(gate_pass, vehicle=vehicle, transporter=transporter, driver=driver)
    gate_pass.save()
    return gate_pass


def _apply_transport(gate_pass, *, vehicle=None, transporter=None, driver=None):
    """Set the FKs and take the text snapshot alongside them."""
    if vehicle is not None:
        gate_pass.vehicle = vehicle
        gate_pass.vehicle_no = vehicle.vehicle_number or ""
        # A vehicle usually knows its own transporter; only fall back to it so an
        # explicitly chosen transporter is never overwritten.
        if transporter is None and getattr(vehicle, "transporter_id", None):
            transporter = vehicle.transporter
    if transporter is not None:
        gate_pass.transporter = transporter
        gate_pass.transporter_name = transporter.name or ""
        gate_pass.transporter_gstin = getattr(transporter, "gstin", "") or ""
    if driver is not None:
        gate_pass.driver = driver
        gate_pass.driver_name = driver.name or ""
        gate_pass.driver_mobile_no = driver.mobile_no or ""
        gate_pass.driver_license_no = driver.license_no or ""


@transaction.atomic
def update_transport(company, gate_pass_id, *, user, vehicle=None, transporter=None, driver=None):
    """Correct the vehicle / driver on a trip that has not left yet."""
    gate_pass = _get_pass(company, gate_pass_id, for_update=True)
    _assert_open(gate_pass, "change the vehicle on")
    _apply_transport(gate_pass, vehicle=vehicle, transporter=transporter, driver=driver)
    gate_pass.updated_by = user
    gate_pass.save()
    return gate_pass


def _assert_open(gate_pass, action):
    if gate_pass.status == MarketplaceGatePassStatus.DISPATCHED:
        raise MarketplaceError(
            f"This trip has already left the gate — you cannot {action} it.",
            code="ALREADY_DISPATCHED",
        )
    if gate_pass.status == MarketplaceGatePassStatus.CANCELLED:
        raise MarketplaceError(
            f"This trip was cancelled — you cannot {action} it.", code="CANCELLED",
        )


@transaction.atomic
def record_weighment(
    company, gate_pass_id, *, user, tare_weight=None, gross_weight=None,
    weighbridge_slip_no=None,
):
    """Record the weighbridge readings.

    Either half may be entered on its own — the empty vehicle is weighed before
    loading and the loaded one after — so each is timestamped as it arrives and
    the net is only derived once both are in.
    """
    gate_pass = _get_pass(company, gate_pass_id, for_update=True)
    _assert_open(gate_pass, "weigh")

    now = timezone.now()
    if tare_weight is not None:
        if tare_weight < 0:
            raise MarketplaceError("Tare weight cannot be negative.", code="INVALID_WEIGHT")
        gate_pass.tare_weight = tare_weight
        gate_pass.first_weighment_at = now
    if gross_weight is not None:
        if gross_weight <= 0:
            raise MarketplaceError(
                "Gross weight must be greater than zero.", code="INVALID_WEIGHT")
        gate_pass.gross_weight = gross_weight
        gate_pass.second_weighment_at = now
    if weighbridge_slip_no is not None:
        gate_pass.weighbridge_slip_no = weighbridge_slip_no.strip()[:50]

    if (
        gate_pass.tare_weight is not None
        and gate_pass.gross_weight is not None
        and gate_pass.tare_weight > gate_pass.gross_weight
    ):
        raise MarketplaceError(
            "Tare weight cannot be greater than gross weight.", code="INVALID_WEIGHT")

    gate_pass.updated_by = user
    # save() derives the net; only then is the pass genuinely weighed.
    gate_pass.save()
    if gate_pass.is_weighed and gate_pass.status == MarketplaceGatePassStatus.DRAFT:
        gate_pass.status = MarketplaceGatePassStatus.WEIGHED
        gate_pass.save(update_fields=["status", "updated_at"])
    return gate_pass


def weight_error(gate_pass):
    """Why this trip cannot leave on weight grounds, or '' if it can.

    Mirrors ``get_dispatch_weight_error`` on the sales side: a load leaves only
    once the vehicle has been weighed both empty and full.
    """
    if gate_pass.gross_weight is None or gate_pass.gross_weight <= 0:
        return "Gross weight is required before this trip can be marked out."
    if gate_pass.tare_weight is None or gate_pass.tare_weight < 0:
        return "Tare weight is required before this trip can be marked out."
    if gate_pass.tare_weight > gate_pass.gross_weight:
        return "Tare weight cannot be greater than gross weight."
    return ""


def _assign_gatepass_no(gate_pass, company):
    """Take the next number in the company's financial-year sequence.

    Taken as the trip leaves rather than when a draft is opened, so an abandoned
    draft never burns one.
    """
    from ..models import MarketplaceGatepassSequence

    gate_pass.gatepass_no = MarketplaceGatepassSequence.next_gatepass_no(company)
    gate_pass.random_code = secrets.token_hex(4).upper()
    gate_pass.qr_payload = f"{gate_pass.gatepass_no}|{gate_pass.random_code}"


@transaction.atomic
def print_gatepass(company, gate_pass_id, *, user):
    """Record that the gatepass was printed.

    Printing does NOT gate the flow: a trip is weighed and marked out, and the
    pass the driver carries can be printed after the truck has gone. This only
    stamps who printed it and when, assigning a number first if the trip somehow
    has none. Reprinting keeps the original number — the pass in the driver's
    hand and the record must agree.
    """
    gate_pass = _get_pass(company, gate_pass_id, for_update=True)
    if gate_pass.status == MarketplaceGatePassStatus.CANCELLED:
        raise MarketplaceError(
            "This trip was cancelled — you cannot print it.", code="CANCELLED")

    if not gate_pass.vehicle_id and not gate_pass.vehicle_no:
        raise MarketplaceError(
            "Record the vehicle before printing the gatepass.", code="NO_VEHICLE")

    if not gate_pass.gatepass_no:
        _assign_gatepass_no(gate_pass, company)
    # A trip that has already gone stays DISPATCHED; printing must not walk its
    # status backwards.
    if gate_pass.status != MarketplaceGatePassStatus.DISPATCHED:
        gate_pass.status = MarketplaceGatePassStatus.GATEPASS_PRINTED
    gate_pass.printed_by = user
    gate_pass.printed_at = timezone.now()
    gate_pass.updated_by = user
    gate_pass.save()
    return gate_pass


@transaction.atomic
def dispatch_out(company, gate_pass_id, *, user, security_name="", out_date=None, out_time=None):
    """Mark the trip out at the gate and stamp the parcels it took.

    Stamping is what stops a parcel riding a second trip, and the counts are
    frozen here because the sheet keeps changing while the trip does not.
    """
    gate_pass = _get_pass(company, gate_pass_id, for_update=True)
    _assert_open(gate_pass, "dispatch")

    error = weight_error(gate_pass)
    if error:
        raise MarketplaceError(error, code="WEIGHT_REQUIRED")

    dispatches = list(
        eligible_dispatches(company, gate_pass.channel, gate_pass.import_batch_id)
    )
    if not dispatches:
        raise MarketplaceError(
            "No gate-approved parcels are left on this sheet to send out.",
            code="NOTHING_TO_DISPATCH",
        )

    # The number is assigned as the trip leaves rather than gating the flow on a
    # print step. Printing is what the driver carries, and it can be done after
    # the truck has gone; the record must not wait on it.
    if not gate_pass.gatepass_no:
        _assign_gatepass_no(gate_pass, company)

    now = timezone.now()
    load = _load_of(dispatches)
    MarketplaceDispatch.objects.filter(id__in=[d.id for d in dispatches]).update(
        gate_pass=gate_pass, updated_by=user, updated_at=now,
    )

    gate_pass.order_count = load["order_count"]
    gate_pass.parcel_count = load["parcel_count"]
    gate_pass.status = MarketplaceGatePassStatus.DISPATCHED
    gate_pass.gate_out_date = out_date or timezone.localdate()
    gate_pass.out_time = out_time or now.astimezone().time().replace(microsecond=0)
    gate_pass.security_name = (security_name or "").strip()[:100]
    gate_pass.dispatched_by = user
    gate_pass.dispatched_at = now
    gate_pass.updated_by = user
    gate_pass.save()
    return gate_pass


@transaction.atomic
def cancel_gate_pass(company, gate_pass_id, *, user, reason=""):
    """Cancel a trip that never left. Its parcels return to the waiting list."""
    gate_pass = _get_pass(company, gate_pass_id, for_update=True)
    _assert_open(gate_pass, "cancel")
    if not (reason or "").strip():
        raise MarketplaceError("A reason is required to cancel a gate pass.", code="NO_REASON")

    gate_pass.status = MarketplaceGatePassStatus.CANCELLED
    gate_pass.cancel_reason = reason.strip()
    gate_pass.cancelled_by = user
    gate_pass.cancelled_at = timezone.now()
    gate_pass.updated_by = user
    gate_pass.save()
    return gate_pass
