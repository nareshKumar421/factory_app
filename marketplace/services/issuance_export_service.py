"""Issuance register export — 'which sheet did we issue what material against'.

Emits a CSV (opens directly in Excel; no extra dependency) linking the uploaded
sheet → its issue request(s) → each consolidated item and its required / approved
/ issued / received quantities. See ``MARKETPLACE_FLIPKART_SHEET_FLOW.md`` §5.5.
"""
import csv
import io

HEADER = [
    "Sheet", "Batch ID", "Uploaded At", "Issue Request", "Warehouse",
    "Request Status", "Item Code", "Item Name", "Type", "UOM",
    "Required", "Approved", "Issued", "Received", "Line Status", "Source SKUs",
]


def build_csv(batch):
    """Return the issuance register for a batch as CSV text."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADER)

    requests = batch.issue_requests.all().prefetch_related("lines")
    uploaded = batch.created_at.isoformat() if batch.created_at else ""
    for request in requests:
        for line in request.lines.all():
            writer.writerow([
                batch.filename, batch.id, uploaded,
                f"MPIR-{request.id}", request.sap_warehouse_code, request.status,
                line.item_code, line.item_name, line.component_type, line.uom,
                _num(line.required_qty), _num(line.approved_qty),
                _num(line.issued_qty), _num(line.received_qty),
                line.status, ", ".join(line.source_skus or []),
            ])
    return buf.getvalue()


def _num(value):
    return f"{value:g}" if value is not None else ""
