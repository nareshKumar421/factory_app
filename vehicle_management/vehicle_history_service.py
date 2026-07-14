"""Aggregate everything the system already knows about a registered vehicle.

Powers the "Previously Registered Vehicle" page: given a registration number, it
returns the vehicle master (incl. dimensions), the most recent driver, the last
visit date, the visit history, and photos captured on earlier visits — pulled
from the reverse relations that all FK back to the single ``Vehicle`` row.
"""
from vehicle_management.models import Vehicle

VISIT_LIMIT = 30
PHOTO_LIMIT = 60


def _abs(f, request):
    if not f:
        return None
    try:
        url = f.url
    except ValueError:
        return None
    return request.build_absolute_uri(url) if request is not None else url


def build_vehicle_history(vehicle_number, *, request=None):
    """Return the full history payload for a registration number.

    ``{"found": False, "vehicle_number": ...}`` when the vehicle isn't registered,
    so the caller can offer to create it.
    """
    reg = (vehicle_number or "").strip()
    vehicle = (
        Vehicle.objects.select_related("vehicle_type", "transporter")
        .filter(vehicle_number__iexact=reg)
        .first()
    )
    if vehicle is None:
        return {"found": False, "vehicle_number": reg}

    entries = list(
        vehicle.driver_gate_entries.select_related("driver")
        .prefetch_related("attachments")
        .order_by("-entry_time")[:VISIT_LIMIT]
    )
    gate_outs = list(
        vehicle.sales_dispatch_gate_outs.order_by("-docked_at")[:VISIT_LIMIT]
    )

    latest_driver = next((e.driver for e in entries if e.driver_id), None)

    dates = [e.entry_time for e in entries if e.entry_time]
    dates += [g.docked_at for g in gate_outs if g.docked_at]
    last_visit = max(dates) if dates else None

    t = vehicle.transporter
    payload = {
        "found": True,
        "vehicle": {
            "id": vehicle.id,
            "vehicle_number": vehicle.vehicle_number,
            "vehicle_type": vehicle.vehicle_type.name if vehicle.vehicle_type else "",
            "capacity_ton": vehicle.capacity_ton,
            "length_m": vehicle.length_m,
            "width_m": vehicle.width_m,
            "height_m": vehicle.height_m,
            "transporter_name": t.name if t else "",
            "transporter_contact": getattr(t, "contact_person", "") if t else "",
            "transporter_mobile": getattr(t, "mobile_no", "") if t else "",
            "transporter_gstin": getattr(t, "gstin", "") if t else "",
            "registered_on": vehicle.created_at,
        },
        "driver": (
            {
                "name": latest_driver.name,
                "mobile_no": latest_driver.mobile_no,
                "license_no": latest_driver.license_no,
                "id_proof_type": latest_driver.id_proof_type,
                "id_proof_number": latest_driver.id_proof_number,
                "photo": _abs(latest_driver.photo, request),
            }
            if latest_driver
            else None
        ),
        "last_visit_date": last_visit,
        "visit_count": vehicle.driver_gate_entries.count(),
        "visits": [
            {
                "entry_no": e.entry_no,
                "entry_time": e.entry_time,
                "entry_type": e.entry_type,
                "status": e.status,
                "driver_name": e.driver.name if e.driver_id else "",
                "photo_count": e.attachments.count(),
            }
            for e in entries
        ],
        "photos": _collect_photos(latest_driver, entries, gate_outs, request),
    }
    return payload


def _collect_photos(latest_driver, entries, gate_outs, request):
    photos = []
    if latest_driver and latest_driver.photo:
        url = _abs(latest_driver.photo, request)
        if url:
            photos.append(
                {"url": url, "kind": "driver", "captured_at": None, "label": f"Driver · {latest_driver.name}"}
            )
    for e in entries:
        for att in e.attachments.all():
            url = _abs(att.file, request)
            if url:
                photos.append(
                    {"url": url, "kind": "gate", "captured_at": att.uploaded_at, "label": f"Visit {e.entry_no}"}
                )
    for g in gate_outs:
        url = _abs(g.truck_photo, request)
        if url:
            photos.append(
                {"url": url, "kind": "dispatch", "captured_at": g.docked_at, "label": f"Dispatch {g.gate_out_date or ''}".strip()}
            )
    return photos[:PHOTO_LIMIT]
