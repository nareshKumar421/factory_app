"""Read a vendor's bill on this server: the printed text, and the Rate Check line.

The bills arrive as CamScanner PDFs, one photographed page with no text layer,
so the page is OCR'd. RapidOCR runs PaddleOCR's PP-OCR models on ONNX Runtime,
on the CPU, with the models shipped inside the pinned wheel: nothing leaves the
server and nothing is downloaded at run time. On 2026-10-09 it read every
printed field of the four sample bills (invoice and PO numbers, GSTINs, GST
rates and amounts) in about five seconds a page.

The reader does not try to understand the bill. It returns what is printed
where, row by row, and ``checks.py`` looks in that for what the GRPO says
should be there. Knowing the answer makes the reading a matter of finding it.

Handwriting is the exception OCR is poor at, and the one mark the checklist
needs is a signature. That is judged from the pixels instead: the "Rate Check"
label is found by OCR, the stamp's own printed rule beside it is erased, and
whatever ink is left is measured against the label's height. A blank line left
nothing (0.00–0.13 on the samples); a pen signature left about 1.0.
"""

import logging
import re
import threading
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

#: What the bill may be, by extension.
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

RENDER_DPI = 200
#: Bills are one page; a few more are read, the rest ignored.
MAX_PAGES = 4
#: Cores one read may use, so a read does not take every core from gunicorn.
OCR_THREADS = 4

#: Ink left on the Rate Check line, per label height squared.
SIGNATURE_BLANK_BELOW = 0.25
SIGNATURE_SIGNED_ABOVE = 0.6

RATE_CHECK_LABEL = re.compile(r"rate\s*ch", re.I)
#: The gate's receiving stamp: "G. No." (not "GR/RR No.").
GATE_STAMP_LABEL = re.compile(r"\bG\s*\.\s*:?\s*No", re.I)
#: JIVO's purchase order numbers: nine digits from 2 (220926152).
PO_NUMBER = re.compile(r"(?<!\d)2\d{8}(?!\d)")
#: A handwritten date, day first: 3/10/26, 01-10-2026.
DATE = re.compile(r"\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{2,4}")

_engine = None
_engine_lock = threading.Lock()


class InvoiceReadError(Exception):
    """The bill could not be read; the message is fit to show the user."""


def mime_type_for(filename: str) -> str:
    name = (filename or "").lower()
    for extension, mime in MIME_TYPES.items():
        if name.endswith(extension):
            return mime
    raise InvoiceReadError("Upload the bill as a PDF or a photo (JPG, PNG).")


def engine_name() -> str:
    try:
        return f"RapidOCR {metadata.version('rapidocr')} (PP-OCR, on this server)"
    except metadata.PackageNotFoundError:
        return "RapidOCR"


def read_invoice(content: bytes, filename: str) -> tuple[dict[str, Any], str]:
    """``(what is printed where, the engine that read it)``. Raises ``InvoiceReadError``."""
    images = _page_images(content, filename)
    lines: list[dict] = []
    rate_check: dict = {"found": False}
    for page, image in enumerate(images, start=1):
        page_lines = _ocr(image, page)
        lines.extend(page_lines)
        if not rate_check["found"]:
            rate_check = rate_check_marks(image, page_lines)
    if not lines:
        raise InvoiceReadError("No text could be read off the bill. Is the scan blank or upside down?")
    return {
        "pages": len(images),
        "lines": lines,
        "rows": group_rows(lines),
        "po_numbers": list(dict.fromkeys(m for line in lines for m in PO_NUMBER.findall(line["text"]))),
        "gate_stamp_date": gate_stamp_date(lines),
        "rate_check": rate_check,
    }, engine_name()


def gate_stamp_date(lines: list[dict]) -> str:
    """The date written in the gate's receiving stamp, as written, or ""."""
    for label in (line for line in lines if GATE_STAMP_LABEL.search(line["text"])):
        x0, y0, x1, y1 = label["box"]
        h = max(1, y1 - y0)
        for line in lines:
            bx0, by0, _, by1 = line["box"]
            if (
                line["page"] == label["page"]
                and bx0 >= x0 - 2 * h
                and y0 - 3 * h <= by0 and by1 <= y1 + 6 * h
            ):
                match = DATE.search(line["text"])
                if match:
                    return match.group(0)
    return ""


# ---------------------------------------------------------------------------
# Pages and OCR
# ---------------------------------------------------------------------------

def _page_images(content: bytes, filename: str) -> list:
    import cv2
    import numpy as np

    if mime_type_for(filename) == "application/pdf":
        import pypdfium2 as pdfium

        try:
            pdf = pdfium.PdfDocument(content)
        except pdfium.PdfiumError as exc:
            raise InvoiceReadError("The PDF could not be opened. Is it damaged or password-protected?") from exc
        images = []
        for index in range(min(len(pdf), MAX_PAGES)):
            bitmap = pdf[index].render(scale=RENDER_DPI / 72)
            image = bitmap.to_numpy()
            images.append(np.ascontiguousarray(image[:, :, :3]))
        return images

    image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise InvoiceReadError("The photo could not be opened.")
    return [image]


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        _engine = RapidOCR(params={
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": OCR_THREADS,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
    return _engine


def _ocr(image, page: int) -> list[dict]:
    with _engine_lock:
        result = _get_engine()(image)
    lines = []
    for box, text, score in zip(result.boxes if result.boxes is not None else [], result.txts or (), result.scores or ()):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        lines.append({
            "page": page,
            "text": str(text),
            "score": round(float(score), 3),
            "box": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
        })
    return lines


def group_rows(lines: list[dict]) -> list[dict]:
    """Join the boxes that sit on one printed line, left to right.

    OCR returns a bill's table cells as separate boxes; "Add : CGST", "@",
    "9.00 %" and "9,161.00" are one row to a reader and have to be one row to
    the checks.
    """
    rows: list[dict] = []
    for line in sorted(lines, key=lambda l: (l["page"], (l["box"][1] + l["box"][3]) / 2)):
        x0, y0, x1, y1 = line["box"]
        middle, height = (y0 + y1) / 2, max(1, y1 - y0)
        row = rows[-1] if rows else None
        if row and row["page"] == line["page"] and abs(middle - row["y"]) <= 0.5 * max(height, row["h"]):
            row["parts"].append(line)
        else:
            rows.append({"page": line["page"], "y": middle, "h": height, "parts": [line]})
    return [
        {
            "page": row["page"],
            "y": int(row["y"]),
            "text": " ".join(part["text"] for part in sorted(row["parts"], key=lambda p: p["box"][0])),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# The Rate Check line
# ---------------------------------------------------------------------------

def rate_check_marks(image, page_lines: list[dict]) -> dict:
    """How much was written on the stamp's "Rate Check" line, and what OCR made of it."""
    import cv2
    import numpy as np

    label = next((line for line in page_lines if RATE_CHECK_LABEL.search(line["text"])), None)
    if label is None:
        return {"found": False}
    x0, y0, x1, y1 = label["box"]
    h = max(1, y1 - y0)
    top, bottom = max(0, y0 - int(0.7 * h)), min(image.shape[0], y1 + int(0.4 * h))
    band = image[top:bottom, x1: min(image.shape[1], x1 + 700)]
    if band.size == 0:
        return {"found": True, "ink": None, "signed": None, "text": ""}

    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    ink = (gray < 150).astype(np.uint8) * 255
    # The stamp prints a long rule after the label; find where it ends, so the
    # gate stamp or the printed text further right is not counted.
    rule = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (45, 1)))
    _, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(rule, np.ones((7, 25), np.uint8)))
    runs = [s for s in stats[1:] if s[cv2.CC_STAT_LEFT] < 120 and s[cv2.CC_STAT_WIDTH] > 120]
    if not runs:
        return {"found": True, "ink": None, "signed": None, "text": ""}
    right = max(int(s[cv2.CC_STAT_LEFT] + s[cv2.CC_STAT_WIDTH]) for s in runs) + 30

    ink, rule = ink[:, :right], rule[:, :right]
    rule = cv2.dilate(rule, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    rest = cv2.subtract(ink, rule)
    rest = cv2.morphologyEx(rest, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    _, _, stats, _ = cv2.connectedComponentsWithStats(rest)
    strokes = [
        s for s in stats[1:]
        if s[cv2.CC_STAT_HEIGHT] >= 0.4 * h or s[cv2.CC_STAT_WIDTH] >= 1.5 * h
    ]
    amount = round(sum(int(s[cv2.CC_STAT_AREA]) for s in strokes) / (h * h), 2)

    # Anything OCR read inside the rule's span is somebody's writing.
    written = " ".join(
        line["text"] for line in page_lines
        if line is not label
        and line["box"][0] >= x1 - 5 and line["box"][2] <= x1 + right + 5
        and line["box"][1] >= top - 5 and line["box"][3] <= bottom + 5
    )
    if amount < SIGNATURE_BLANK_BELOW:
        signed = False
    elif amount > SIGNATURE_SIGNED_ABOVE:
        signed = True
    else:
        signed = None
    return {"found": True, "ink": amount, "signed": signed, "text": written}
