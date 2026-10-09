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

Small vendors' bill books print the GST lines with a dotted blank for the rate,
"+SGST@..........%", and the rate is written in by hand. OCR reads the dots
and drops the handwritten 9 among them. Such a line is read a second time with
the printed rules and dots erased (``fill_rate_blanks``). Their figures are
written closed with "/-" (no paise), whose stroke OCR reads as a 1: 420/- comes
back "4201-" and is put right (``mend_slash_dashes``).
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
#: A GST line printed with a blank for its rate: "+SGST@..........%".
GST_RATE_BLANK = re.compile(r"(?:C|S|I|UT)\s*GST\s*@", re.I)
#: The rate in a re-read blank: "+SGST@ 9 %", "+CGST@ 9.%", "@ 2.5%".
RATE_IN_BLANK = re.compile(r"@[^\d%]*(\d{1,2}(?:\.\d{1,2})?)")
#: How sure the recognizer must be of a re-read blank to use it.
BLANK_MIN_SCORE = 0.5
#: A figure closed with "/-", its stroke read as a 1 or a bar: "4201-" for
#: 420/-, "0.281-" for 0.28/-, "75.61-" for 75.6/-. Not a date: 21-10-26.
SLASH_DASH = re.compile(r"(?<![\w.,])(\d[\d,]*(?:\.\d+)?)[1|lI/\\!\]]\s*[-–—~_]+(?![\w.,])")

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
        page_lines = mend_slash_dashes(fill_rate_blanks(image, _ocr(image, page)))
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
        # RapidOCR keeps a call's use_* flags for the calls after it, so a
        # ``_recognize`` would otherwise leave the engine finding no text.
        result = _get_engine()(image, use_det=True, use_cls=True, use_rec=True)
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


def _recognize(image) -> tuple[str, float]:
    """Read one line of text that is already cut out: no finding, no turning."""
    with _engine_lock:
        result = _get_engine()(image, use_det=False, use_cls=False, use_rec=True)
    if not result.txts:
        return "", 0.0
    return str(result.txts[0]), float(result.scores[0])


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
# GST rates written into a printed blank
# ---------------------------------------------------------------------------

def fill_rate_blanks(image, page_lines: list[dict]) -> list[dict]:
    """Read the GST rate a person wrote into a printed "+SGST@.....%" blank.

    OCR finds such a line but reads it as the printed dots, with the
    handwritten 9 dropped among them (GRAPHIC-368, 2026-10-09); the "%" may
    come as a box of its own. Cut out from the label to the "%", with the
    table's rules and the dots erased, the same recognizer reads "+SGST@ 9 %".
    That replaces the line, and any box of dots it covered, only when a rate
    is read with confidence. A blank nobody wrote in stays as OCR read it.
    """
    lines = list(page_lines)
    for line in page_lines:
        if line not in lines:
            continue
        match = GST_RATE_BLANK.search(line["text"])
        if not match:
            continue
        x0, y0, x1, y1 = line["box"]
        h = max(1, y1 - y0)
        middle = (y0 + y1) / 2
        after = sorted(
            (
                other for other in lines
                if other is not line and other["page"] == line["page"]
                and other["box"][0] >= x1 - 5
                and abs((other["box"][1] + other["box"][3]) / 2 - middle) <= 0.5 * h
            ),
            key=lambda other: other["box"][0],
        )
        # The blank runs to the "%": in the line's own box, or in a box of its
        # own further on, with whatever OCR made of the blank in between.
        percent = next(
            (other for other in after if "%" in other["text"] and other["box"][0] - x1 <= 10 * h), None,
        )
        if "%" in line["text"][match.end():]:
            covered, end = [], x1
        elif percent is not None:
            covered = [other for other in after if other["box"][0] <= percent["box"][0]]
            end = percent["box"][2]
        else:
            # No "%" found: up to what is printed next on the row.
            covered = []
            end = min(x1 + 6 * h, after[0]["box"][0] - 5 if after else image.shape[1])
        read = " ".join([line["text"]] + [other["text"] for other in covered])
        if re.search(r"@\s*\d|\d\s*%", read):
            continue  # Printed, or read, as the checks can take it.
        end = min(end, image.shape[1])
        top = min([y0] + [other["box"][1] for other in covered])
        bottom = max([y1] + [other["box"][3] for other in covered])
        # A digit OCR did read, but among the dots ("+SGST@.. 9 ..%"), is used
        # as it is; otherwise the blank is read again.
        rate, score = RATE_IN_BLANK.search(read[match.start():]), line["score"]
        if rate is None and end > x0:
            crop = image[max(0, top - h // 4): min(image.shape[0], bottom + h // 10), x0:end]
            text, score = _recognize(_erase_printed_leaders(crop, h))
            rate = RATE_IN_BLANK.search(text)
        if rate is None or score < BLANK_MIN_SCORE:
            continue
        lines[lines.index(line)] = {
            "page": line["page"],
            "text": f"{line['text'][:match.end()]} {rate.group(1)} %",
            "score": round(score, 3),
            "box": [x0, top, end, bottom],
            # What OCR read before the blank was filled in.
            "ocr_text": read,
        }
        lines = [other for other in lines if not any(other is c for c in covered)]
    return lines


def _erase_printed_leaders(crop, h: int):
    """The cut-out line, black on white, without the table's rules or the dots.

    A dot is small beside the line's letters. One sitting close between two
    strokes is kept: the point of a handwritten 2.5.
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = (gray < 150).astype(np.uint8) * 255
    horizontal = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(1.5 * h)), 1))
    vertical = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.8 * ink.shape[0]))))
    rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal) | cv2.morphologyEx(ink, cv2.MORPH_OPEN, vertical)
    ink = cv2.subtract(ink, cv2.dilate(rules, np.ones((3, 3), np.uint8)))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink)
    keep = np.zeros(count, bool)
    if count > 1:
        heights = [int(s[cv2.CC_STAT_HEIGHT]) for s in stats[1:]]
        letter = float(np.median([x for x in heights if x >= 0.5 * max(heights)]))
        dot, near = 0.45 * letter, 0.6 * letter

        def size(s):
            return max(s[cv2.CC_STAT_WIDTH], s[cv2.CC_STAT_HEIGHT])

        strokes = [
            (int(s[cv2.CC_STAT_LEFT]), int(s[cv2.CC_STAT_LEFT] + s[cv2.CC_STAT_WIDTH]))
            for s in stats[1:] if size(s) > dot
        ]
        for index in range(1, count):
            s = stats[index]
            if size(s) > dot:
                keep[index] = True
                continue
            left, right = int(s[cv2.CC_STAT_LEFT]), int(s[cv2.CC_STAT_LEFT] + s[cv2.CC_STAT_WIDTH])
            keep[index] = (
                any(r <= right and left - r <= near for _, r in strokes)
                and any(l >= left and l - right <= near for l, _ in strokes)
            )
    clean = np.where(keep[labels], 0, 255).astype(np.uint8)
    return cv2.cvtColor(clean, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------------------
# Figures closed with "/-"
# ---------------------------------------------------------------------------

def mend_slash_dashes(page_lines: list[dict]) -> list[dict]:
    """Put back the "/-" a handwritten figure was closed with.

    OCR reads its stroke as a 1 (GRAPHIC-368: 420/- as "4201-", 0.28/- as
    "0.281-"), which makes 420 into 4201. A figure printed "4,201/-" reads
    the same before and after.
    """
    mended = []
    for line in page_lines:
        text = SLASH_DASH.sub(lambda m: f"{m.group(1)}/-", line["text"])
        if text != line["text"]:
            line = {**line, "text": text, "ocr_text": line.get("ocr_text", line["text"])}
        mended.append(line)
    return mended


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
