import json
import logging
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from gate_core.models import SalesDispatchGateOut
from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

logger = logging.getLogger(__name__)


class GatepassPdfError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatepassPdfResult:
    pdf: bytes
    filename: str
    parameters: Dict[str, Any]
    renderer: str


def render_sales_dispatch_gatepass_pdf(entry: SalesDispatchGateOut) -> GatepassPdfResult:
    parameters = build_crystal_parameters(entry)
    filename = build_gatepass_pdf_filename(entry)

    if crystal_renderer_configured():
        return GatepassPdfResult(
            pdf=render_with_crystal_runtime(parameters),
            filename=filename,
            parameters=parameters,
            renderer="crystal",
        )

    if crystal_renderer_requested():
        raise GatepassPdfError(
            "SAP Crystal gatepass renderer is partially configured. Check "
            "SAP_GATEPASS_CRYSTAL_REPORT_PATH and SAP_GATEPASS_CRYSTAL_RENDERER_COMMAND."
        )

    if not settings.SAP_GATEPASS_CRYSTAL_ENABLE_FALLBACK_PDF:
        raise GatepassPdfError(
            "SAP Crystal gatepass renderer is not configured. Set "
            "SAP_GATEPASS_CRYSTAL_REPORT_PATH and SAP_GATEPASS_CRYSTAL_RENDERER_COMMAND."
        )

    live_document = fetch_live_sap_document(entry)
    return GatepassPdfResult(
        pdf=render_fallback_pdf(entry, live_document),
        filename=filename,
        parameters=parameters,
        renderer="fallback",
    )


def build_crystal_parameters(entry: SalesDispatchGateOut) -> Dict[str, Any]:
    object_id = settings.SAP_GATEPASS_CRYSTAL_OBJECT_IDS.get(
        entry.document_type,
        settings.SAP_GATEPASS_CRYSTAL_OBJECT_IDS["INVOICE"],
    )
    return {
        "DocKey@": int(entry.sap_doc_entry),
        "ObjectId@": str(object_id),
    }


def crystal_renderer_configured() -> bool:
    report_path = str(settings.SAP_GATEPASS_CRYSTAL_REPORT_PATH or "").strip()
    renderer_command = str(settings.SAP_GATEPASS_CRYSTAL_RENDERER_COMMAND or "").strip()
    return bool(report_path and renderer_command and Path(report_path).exists())


def crystal_renderer_requested() -> bool:
    report_path = str(settings.SAP_GATEPASS_CRYSTAL_REPORT_PATH or "").strip()
    renderer_command = str(settings.SAP_GATEPASS_CRYSTAL_RENDERER_COMMAND or "").strip()
    return bool(report_path or renderer_command)


def render_with_crystal_runtime(parameters: Dict[str, Any]) -> bytes:
    command = build_renderer_command()
    report_path = str(settings.SAP_GATEPASS_CRYSTAL_REPORT_PATH)

    with tempfile.TemporaryDirectory(prefix="gatepass_crystal_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        output_path = tmp_path / "gatepass.pdf"
        params_path = tmp_path / "parameters.json"
        params_path.write_text(json.dumps(parameters), encoding="utf-8")

        args = [
            *command,
            "--report",
            report_path,
            "--output",
            str(output_path),
            "--params-json",
            str(params_path),
        ]
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=int(settings.SAP_GATEPASS_CRYSTAL_RENDER_TIMEOUT_SECONDS),
        )
        if completed.returncode != 0:
            logger.error(
                "Crystal gatepass renderer failed: returncode=%s stdout=%s stderr=%s",
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
            raise GatepassPdfError("SAP Crystal gatepass renderer failed.")
        if not output_path.exists():
            raise GatepassPdfError("SAP Crystal gatepass renderer did not produce a PDF.")

        pdf = output_path.read_bytes()
        if not pdf.startswith(b"%PDF"):
            raise GatepassPdfError("SAP Crystal gatepass renderer output is not a PDF.")
        return pdf


def build_renderer_command() -> List[str]:
    command = str(settings.SAP_GATEPASS_CRYSTAL_RENDERER_COMMAND or "").strip()
    if not command:
        raise GatepassPdfError("SAP Crystal gatepass renderer command is not configured.")
    return shlex.split(command)


def build_gatepass_pdf_filename(entry: SalesDispatchGateOut) -> str:
    reference = entry.sap_doc_num or entry.gatepass_no or entry.entry_no
    safe_reference = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in reference)
    return f"gatepass_{safe_reference}.pdf"


def fetch_live_sap_document(entry: SalesDispatchGateOut) -> Dict[str, Any] | None:
    try:
        return SalesDispatchDocumentService(entry.company).get_document(
            entry.document_type,
            entry.sap_doc_entry,
        )
    except Exception:
        logger.exception(
            "Unable to fetch live SAP document for gatepass PDF: type=%s doc_entry=%s",
            entry.document_type,
            entry.sap_doc_entry,
        )
        return None


def render_fallback_pdf(entry: SalesDispatchGateOut, live_document: Dict[str, Any] | None) -> bytes:
    width, height = 1240, 1754
    margin = 30
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    fonts = load_fonts()

    header_y = margin
    draw.rectangle([margin, margin, width - margin, height - margin], outline="black", width=2)
    draw.rectangle([margin, margin, width - margin, 500], outline="black", width=2)
    draw.rectangle([45, 45, 220, 150], outline="black", width=1)
    draw.text((80, 80), "JIVO", font=fonts["title"], fill="black")
    draw.text((280, header_y + 10), "Bill Summary", font=fonts["title"], fill="black")
    draw.text((570, header_y + 10), company_name(entry), font=fonts["title"], fill="black")

    draw_multiline(
        draw,
        company_address(entry),
        (455, 70),
        fonts["body"],
        max_width=520,
        line_gap=6,
    )
    draw.text((910, 120), "GST No.", font=fonts["body"], fill="black")
    draw.text((980, 120), f": {entry.bp_gstin or '-'}", font=fonts["body"], fill="black")
    draw.text((980, 150), "Phone: 9910836550", font=fonts["body"], fill="black")

    draw.rectangle([margin, 185, 405, 230], fill=(192, 192, 192), outline="black")
    draw.rectangle([405, 185, 810, 230], fill=(192, 192, 192), outline="black")
    draw.rectangle([810, 185, width - margin, 230], fill=(192, 192, 192), outline="black")
    draw.text((35, 202), f"Invoice Number : {entry.sap_doc_num}", font=fonts["bold"], fill="black")
    draw.text((420, 202), f"Invoice Date :{format_date(entry.sap_doc_date)}", font=fonts["bold"], fill="black")
    draw.text((835, 202), f"Dispatch Date: {format_date(get_dispatch_date(entry, live_document))}", font=fonts["bold"], fill="black")

    left_x, right_x = 45, 675
    draw_label_value(draw, "Customer Name", customer_name(entry, live_document), left_x, 260, fonts)
    draw_label_value(draw, "Delivery Address", delivery_address(entry, live_document), left_x, 305, fonts)
    draw_label_value(draw, "Contact No", "", left_x, 445, fonts)
    draw_label_value(draw, "Transporter Name", entry.transporter_name or "Jivo Vehicle", right_x, 260, fonts)
    draw_label_value(draw, "Bilty No", entry.bilty_no or "NA", right_x, 305, fonts)
    draw_label_value(draw, "Bilty Date", entry.bilty_date, right_x, 350, fonts)
    draw_label_value(draw, "Vehicle No", entry.vehicle_no, right_x, 395, fonts)
    draw_label_value(draw, "Driver Contact No", entry.driver_mobile_no, right_x, 440, fonts)
    draw_label_value(draw, "DriverName.", entry.driver_name, right_x, 485, fonts)

    table_top = 560
    columns = [
        (30, 80, "S.\nNo"),
        (80, 520, "Description of Goods"),
        (520, 625, "Qty Pcs"),
        (625, 720, "Box"),
        (720, 830, "Loose Qty"),
        (830, 1070, "Warehouse\nGodown"),
        (1070, 1210, "Gross\nWeight\n(KGS)"),
    ]
    draw.rectangle([30, table_top, 1210, table_top + 65], outline="black", width=2)
    for x1, _x2, heading in columns[1:]:
        draw.line([x1, table_top, x1, 1120], fill="black", width=1)
    for x1, x2, heading in columns:
        draw_multiline(draw, heading, (x1 + 8, table_top + 10), fonts["small_bold"], x2 - x1 - 16)

    rows = table_rows(entry, live_document)
    y = table_top + 65
    for idx, row in enumerate(rows[:10], start=1):
        row_h = 62
        draw.line([30, y, 1210, y], fill="black", width=1)
        draw.text((45, y + 18), str(idx), font=fonts["body"], fill="black")
        draw_multiline(draw, row["description"], (90, y + 12), fonts["body"], 400, line_gap=2)
        draw_right(draw, format_number(row["qty"]), 610, y + 18, fonts["body"])
        draw_right(draw, format_number(row["boxes"]), 705, y + 18, fonts["body"])
        draw_right(draw, format_number(row["loose_qty"]), 815, y + 18, fonts["body"])
        draw.text((850, y + 18), row["warehouse"] or "-", font=fonts["body"], fill="black")
        draw_right(draw, format_number(row["weight"]), 1195, y + 18, fonts["body"])
        y += row_h

    totals = totals_for(entry, live_document, rows)
    draw.line([30, y, 1210, y], fill="black", width=2)
    draw.text((80, y + 20), "Total :", font=fonts["bold"], fill="black")
    draw_right(draw, format_number(totals["qty"]), 610, y + 20, fonts["bold"])
    draw_right(draw, format_number(totals["boxes"]), 705, y + 20, fonts["bold"])
    draw_right(draw, format_number(totals["loose_qty"]), 815, y + 20, fonts["bold"])
    draw_right(draw, format_number(totals["weight"]), 1195, y + 20, fonts["bold"])

    bottom_y = y + 90
    draw.text((45, bottom_y), "Bill Amount", font=fonts["title"], fill="black")
    draw.text((165, bottom_y), f": {format_money(entry.sap_doc_total)}", font=fonts["title"], fill="black")
    draw.text((45, bottom_y + 40), "Total Liter", font=fonts["title"], fill="black")
    draw.text((165, bottom_y + 40), f": {format_number(entry.total_litres)}", font=fonts["title"], fill="black")
    draw.text((360, bottom_y + 40), "Total Gross Weight", font=fonts["title"], fill="black")
    draw.text((560, bottom_y + 40), ": ", font=fonts["title"], fill="black")
    draw.text((615, bottom_y + 40), "KGS", font=fonts["bold"], fill="black")
    draw.text((665, bottom_y + 40), format_number(totals["weight"]), font=fonts["body"], fill="black")
    draw.text((45, bottom_y + 85), "Remarks:", font=fonts["bold"], fill="black")
    draw.text((760, bottom_y + 85), "Dispatched By", font=fonts["small_bold"], fill="black")
    draw.text((980, bottom_y + 85), f"For {company_name(entry)}", font=fonts["small_bold"], fill="black")
    draw.text((980, bottom_y + 210), "Authorised Signatory", font=fonts["small"], fill="black")
    draw.text((880, height - 55), "Printed by SAP Business One", font=fonts["tiny"], fill="black")

    output = BytesIO()
    image.save(output, "PDF", resolution=150.0)
    return output.getvalue()


def load_fonts() -> Dict[str, ImageFont.ImageFont]:
    font_paths = [
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    bold_paths = [
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "arialbd.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    font_path = next((path for path in font_paths if path.exists()), None)
    bold_path = next((path for path in bold_paths if path.exists()), font_path)

    def font(size: int, bold: bool = False):
        path = bold_path if bold else font_path
        if path:
            return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    return {
        "title": font(22, True),
        "bold": font(18, True),
        "body": font(18),
        "small_bold": font(15, True),
        "small": font(14),
        "tiny": font(12),
    }


def company_name(entry: SalesDispatchGateOut) -> str:
    return entry.sap_branch_name or entry.company.name


def company_address(entry: SalesDispatchGateOut) -> str:
    return entry.sap_comments or entry.ship_to_address or entry.place_of_supply or ""


def customer_name(entry: SalesDispatchGateOut, live_document: Dict[str, Any] | None) -> str:
    return entry.customer_name or (live_document or {}).get("card_name", "")


def delivery_address(entry: SalesDispatchGateOut, live_document: Dict[str, Any] | None) -> str:
    return entry.ship_to_address or (live_document or {}).get("ship_to_address", "")


def get_dispatch_date(entry: SalesDispatchGateOut, live_document: Dict[str, Any] | None) -> Any:
    return (live_document or {}).get("dispatch_date") or getattr(entry.dispatch_plan, "dispatch_date", None)


def table_rows(entry: SalesDispatchGateOut, live_document: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    items = list(entry.items.all())
    if not items and live_document:
        items = live_document.get("items", [])

    rows = []
    for item in items:
        get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
        quantity = get("quantity", 0)
        boxes = get("total_boxes", 0)
        rows.append(
            {
                "description": get("item_name", "") or get("item_code", ""),
                "qty": quantity,
                "boxes": boxes,
                "loose_qty": 0,
                "warehouse": get("warehouse_code", "") or get("from_warehouse", "") or get("to_warehouse", ""),
                "weight": get("total_weight", 0),
            }
        )
    return rows


def totals_for(
    entry: SalesDispatchGateOut,
    live_document: Dict[str, Any] | None,
    rows: Iterable[Dict[str, Any]],
) -> Dict[str, Decimal]:
    row_list = list(rows)
    return {
        "qty": decimal_or_sum(entry.total_quantity, row_list, "qty"),
        "boxes": decimal_or_sum(entry.total_boxes, row_list, "boxes"),
        "loose_qty": Decimal("0"),
        "weight": decimal_or_sum(entry.total_weight, row_list, "weight"),
    }


def decimal_or_sum(value: Any, rows: Iterable[Dict[str, Any]], key: str) -> Decimal:
    if value not in (None, ""):
        return to_decimal(value)
    total = Decimal("0")
    for row in rows:
        total += to_decimal(row.get(key))
    return total


def to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def format_number(value: Any) -> str:
    number = to_decimal(value)
    return f"{number:,.2f}"


def format_money(value: Any) -> str:
    return format_number(value)


def format_date(value: Any) -> str:
    if isinstance(value, datetime):
        value = timezone.localtime(value).date()
    if isinstance(value, date):
        return f"{value.day}/{value.month}/{value.year}"
    if value in (None, ""):
        return ""
    return str(value)


def draw_label_value(draw, label: str, value: Any, x: int, y: int, fonts: Dict[str, ImageFont.ImageFont]):
    draw.text((x, y), label, font=fonts["bold"], fill="black")
    draw.text((x + 185, y + 2), ":", font=fonts["body"], fill="black")
    draw_multiline(draw, format_date(value) if isinstance(value, (date, datetime)) else str(value or ""), (x + 210, y + 2), fonts["body"], 350)


def draw_multiline(draw, text: str, xy: tuple[int, int], font, max_width: int, line_gap: int = 4):
    x, y = xy
    lines = wrap_text(draw, text or "", font, max_width)
    for line in lines:
        draw.text((x, y), line, font=font, fill="black")
        y += text_height(font) + line_gap


def wrap_text(draw, text: str, font, max_width: int) -> List[str]:
    lines: List[str] = []
    for raw_line in str(text).splitlines() or [""]:
        words = raw_line.split()
        if not words:
            lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if text_width(draw, candidate, font) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines[:5]


def draw_right(draw, text: str, right_x: int, y: int, font):
    draw.text((right_x - text_width(draw, text, font), y), text, font=font, fill="black")


def text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def text_height(font) -> int:
    bbox = font.getbbox("Ag")
    return bbox[3] - bbox[1]
