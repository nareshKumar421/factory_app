"""Kulbeer's signature on the bill's Rate Check line.

The bill reaches accounts with JIVO's check stamp on it: "Quantity Check",
"Quality Check" and "Rate Check", each followed by a printed rule, signed by
whoever checked that. Kulbeer signs the Rate Check rule once he has held the
bill's rates to the PO. The scans attached at GRPO time are mostly taken
before that (167 of the 169 Rate Check lines on Sep-Oct 2026's were blank);
the bill uploaded for the A/P draft carries his signature. The gate's own
stamp, "JIVO WELLNESS PVT. LTD.", has a Signature box too: that is the gate's
mark, not his.

The line is found by its label, which OCR reads loosely ("Rate Chock", "Rato
Check"), or under the stamp's other two labels when it missed this one. Where
the stamp landed on the bill's print and OCR read the two as one, the page is
read again with only the blue ink left on it (``stamp_ink_only``).

The stamp and the pen are blue and the bill's print is black, so on a colour
scan only the blue is kept: the signature is cut cleanly even where the stamp
landed on the bill's print. The window is turned level by the stamp's rules,
the rules are erased, and the strokes that cross the Rate Check rule are kept
with whatever is written close to them. A blank line leaves nothing; nobody
has to judge faint ink.

Whose it is: the start of the signature, his "K" and the letters after it,
is held by shape to signatures of his kept in ``signatures/``. Only the start
is compared, at the scale of the stamp's printed label: the flourish after
it is never the same twice.
"""

import re
from pathlib import Path

#: The check stamp's three labels, as OCR reads them.
QUANTITY_LABEL = re.compile(r"\bqu?an\w*\s*c[hn]", re.I)
QUALITY_LABEL = re.compile(r"\bqu?al\w*\s*c[hn]", re.I)
RATE_LABEL = re.compile(r"(?:^|\W)[ri]?at[eo]\s*c[hn]", re.I)
#: Rate Check is this many label heights under Quantity Check.
RATE_UNDER_QUANTITY = 3.05

#: Where the signature is looked for, in label heights from the label's left
#: edge (across) and from its top and bottom (up, down).
WINDOW_ACROSS = (4.0, 15.0)
WINDOW_UP, WINDOW_DOWN = 1.6, 1.0
#: Less ink than this across the rule (in label heights squared) is a smudge
#: of the stamp, not a signature. His signatures leave 0.9-3.1.
SIGNED_MIN_INK = 0.4
#: Where the printed rule ends, in label heights from the label's start
#: (9-12.5 on 2026's bills), for when it does not show. Short of the gate
#: stamp, which may sit right after it.
RULE_ENDS = 10

#: His kept signatures, cut as ``body`` cuts them, at a label 40 px high.
REFERENCES = Path(__file__).resolve().parent / "signatures"
REFERENCE_LABEL_HEIGHT = 40
#: The start of a signature: across from its first stroke, and up and down
#: from the rule, in label heights; compared at ``SHAPE_SIZE`` (w, h).
BODY_ACROSS = (-0.2, 4.8)
BODY_RISE = (-2.2, 1.0)
SHAPE_SIZE = (100, 64)
#: How far one shape may be shifted on the other, in pixels of ``SHAPE_SIZE``.
SHAPE_SHIFT = 6


def read_rate_check(image, page_lines: list[dict], signer: str) -> dict:
    """The Rate Check line on this page: ``found`` (its label), ``signed``
    (anything written across it), ``ink`` (how much, in label heights
    squared) and ``match``, how alike the start of it is to ``signer``'s kept
    signatures, 0..1 (None with none kept or nothing written)."""
    label = rate_check_label(page_lines)
    if label is None:
        return {"found": False}
    strokes, h, rule_y = signature_strokes(image, label)
    ink = round(int((strokes > 0).sum()) / (h * h), 2)
    if ink < SIGNED_MIN_INK:
        return {"found": True, "signed": False, "ink": ink, "signer": signer, "match": None}
    shape = body_shape(strokes, h, rule_y)
    kept = reference_shapes(signer)
    match = max((likeness(shape, other) for other in kept), default=None) if shape is not None else None
    return {
        "found": True, "signed": True, "ink": ink, "signer": signer,
        "match": None if match is None else round(match, 3),
    }


def rate_check_label(page_lines: list[dict]):
    """The Rate Check label's box ``[x0, y0, x1, y1]``, or None."""
    quantity = [line for line in page_lines if QUANTITY_LABEL.search(line["text"])]
    quality = [line for line in page_lines if QUALITY_LABEL.search(line["text"])]
    rate = [line for line in page_lines if RATE_LABEL.search(line["text"])]

    def under(upper, lower) -> bool:
        x0, y0, _, y1 = upper["box"]
        h = y1 - y0
        return abs(lower["box"][0] - x0) < 1.5 * h and 0.8 * h < lower["box"][1] - y0 < 3 * h

    for line in rate:
        if any(under(above, line) for above in quality):
            return list(line["box"])
    for middle in quality:
        for top in quantity:
            if under(top, middle):
                step = middle["box"][1] - top["box"][1]
                x0, y0, x1, y1 = middle["box"]
                return [x0, y0 + step, x1, y1 + step]
    if len(rate) == 1:
        return list(rate[0]["box"])
    if len(quantity) == 1:
        x0, y0, x1, y1 = quantity[0]["box"]
        step = round(RATE_UNDER_QUANTITY * (y1 - y0))
        return [x0, y0 + step, x1, y1 + step]
    return None


def stamp_ink_only(image):
    """The page with everything but the stamp's (and pens') blue ink
    whitened, for reading the stamp where it landed on the bill's print."""
    import numpy as np

    page = np.full_like(image, 255)
    blue = _blue_ink(image) > 0
    page[blue] = image[blue]
    return page


def is_colour(image) -> bool:
    """Whether the scan is in colour: some of it is blue stamp or pen ink."""
    return int((_blue_ink(image) > 0).sum()) > 0.001 * image.shape[0] * image.shape[1]


def _blue_ink(image):
    """Stamp and pen ink: blue to violet, darker than the paper around it."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, saturation, value = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    paper = float(np.median(value))
    return ((saturation > 35) & (value < min(235, paper - 25)) & (hue >= 95) & (hue <= 165)).astype(np.uint8) * 255


def _dark_ink(image):
    """Any ink, for a black-and-white scan."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    paper = float(np.median(gray))
    return (gray < min(150, paper - 60)).astype(np.uint8) * 255


def signature_strokes(image, label: list[int]):
    """``(what is written across the line, ink 255 on 0 in the levelled
    window; the label's height; the rule's row in the window)``.

    OCR boxes the label upright, so a tilted label's box is taller than its
    letters: the tilt is found first and the height taken off it."""
    import math

    x0, y0, x1, y1 = label
    angle = _tilt(image, label)
    if angle is None:
        _, _, angle = _window(image, label, y1 - y0)
    h = max(int(0.6 * (y1 - y0)), round((y1 - y0) - (x1 - x0) * abs(math.sin(math.radians(angle)))), 1)
    middle = (y0 + y1) / 2
    label = [x0, round(middle - h / 2), x1, round(middle + h / 2)]
    ink, colour, _ = _window(image, label, h, angle)
    left, top = max(0, int(x0 + WINDOW_ACROSS[0] * h)), max(0, int(label[1] - WINDOW_UP * h))
    strokes, rule_y = _across_the_rule(
        ink, h, (x0 - left, label[1] - top, x1 - left, label[3] - top), table_rules=not colour,
    )
    return strokes, h, rule_y


def _tilt(image, label):
    """The stamp's tilt in degrees, from the long straight lines by its
    labels: its own rules, longer than any stroke of a signature there. None
    if none show."""
    import cv2
    import numpy as np

    x0, y0, _, y1 = label
    h = max(1, y1 - y0)
    window = image[max(0, int(y0 - 3 * h)): int(y1 + 0.6 * h), max(0, x0): int(x0 + 10 * h)]
    if window.size == 0:
        return None
    colour = int((_blue_ink(window) > 0).sum()) > 0.5 * h * h
    found = cv2.HoughLinesP(
        _blue_ink(window) if colour else _dark_ink(window), 1, np.pi / 720,
        threshold=h, minLineLength=int(2.5 * h), maxLineGap=int(0.3 * h),
    )
    lines = [
        (np.degrees(np.arctan2(yb - ya, xb - xa)), np.hypot(xb - xa, yb - ya))
        for xa, ya, xb, yb in (found.reshape(-1, 4) if found is not None else [])
    ]
    lines = sorted((angle, length) for angle, length in lines if abs(angle) <= 12)
    if not lines:
        return None
    # The length-weighted median.
    half, total = sum(length for _, length in lines) / 2, 0.0
    for angle, length in lines:
        total += length
        if total >= half:
            return float(angle)


def _window(image, label, h: int, angle=None):
    """``(the window's ink, levelled; whether it is a colour scan; the angle
    it was turned by)``."""
    x0, y0, x1, y1 = label
    left, right = max(0, int(x0 + WINDOW_ACROSS[0] * h)), min(image.shape[1], int(x0 + WINDOW_ACROSS[1] * h))
    top, bottom = max(0, int(y0 - WINDOW_UP * h)), min(image.shape[0], int(y1 + WINDOW_DOWN * h))
    window = image[top:bottom, left:right]
    colour = int((_blue_ink(window) > 0).sum()) > 0.5 * h * h
    ink = _blue_ink(window) if colour else _dark_ink(window)
    level, angle = _level(ink, h, (x1 - left, (y0 + y1) / 2 - top), angle)
    return level, colour, angle


def _level(ink, h: int, pivot, angle=None):
    """``(the window turned so the stamp's rules lie level, the angle)``.
    The stamp goes on by hand, up to 10 degrees off. It is turned about
    ``pivot``, the label's end, so the rule stays at the label's foot."""
    import cv2
    import numpy as np

    def turned(by):
        turn = cv2.getRotationMatrix2D((float(pivot[0]), float(pivot[1])), float(by), 1.0)
        return cv2.warpAffine(ink, turn, (ink.shape[1], ink.shape[0]), flags=cv2.INTER_NEAREST)

    if angle is not None:
        return turned(angle), angle
    run = cv2.getStructuringElement(cv2.MORPH_RECT, (h, 1))
    best = (-1, 0.0)
    for tried in np.arange(-10, 10.01, 0.5):
        flat = cv2.countNonZero(cv2.morphologyEx(turned(tried), cv2.MORPH_OPEN, run))
        if flat > best[0]:
            best = (flat, float(tried))
    return turned(best[1]), best[1]


def _rule(ink, h: int, label):
    """``(row, first column, last column)`` of the rule printed after the
    label: of the rows by the label's foot, the one whose level ink starts
    by the label's end and runs on furthest over the gaps in its print. The
    gate stamp's border, often level with it further along, starts too far
    off. Where no rule shows (a faint stamp), its usual place: from the
    label's end to ``RULE_ENDS`` label heights from its start."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = label
    runs = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(0.6 * h)), 1)))
    best, band = None, max(2, int(0.15 * h))
    for row in range(max(0, int(y0)), min(runs.shape[0], int(y1 + 0.6 * h))):
        # The printed rule is wavy: take its print within a band.
        columns = np.where(runs[max(0, row - band): row + band + 1].any(0))[0]
        if len(columns) == 0 or columns[0] > x1 + 3 * h:
            continue
        start = end = int(columns[0])
        for column in columns:
            if column - end > 0.6 * h:
                break
            end = int(column)
        if end - start >= 1.5 * h and (best is None or end - start > best[2] - best[1]):
            best = (row, start, end)
    return best or (int(y1 - 0.15 * h), max(0, int(x1)), int(x0 + RULE_ENDS * h))


def _across_the_rule(ink, h: int, label, table_rules: bool):
    """The strokes crossing the Rate Check rule, and those written close to
    them; the stamp's rules, the gate stamp's border and specks left out."""
    import cv2
    import numpy as np

    runs = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (h, 1)))
    row, start, end = _rule(ink, h, label)
    borders = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(1.2 * h))))
    rest = cv2.subtract(ink, cv2.dilate(runs, np.ones((3, 3), np.uint8)))
    rest = cv2.subtract(rest, cv2.dilate(borders, np.ones((3, 5), np.uint8)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(rest)

    def written(index) -> bool:
        x, y, w, sh, _ = stats[index]
        if max(w, sh) < 0.25 * h:
            return False  # a speck
        if sh < 0.2 * h and w > 2 * sh:
            return False  # a rule's ragged edge
        if table_rules and w <= 0.15 * h and sh >= 0.8 * h:
            return False  # a broken piece of the bill's table
        return y + sh >= row - 1.0 * h  # not wholly up by the Quality Check line

    def crosses(index) -> bool:
        # Across the rule, and past its start: not the label's last letter.
        x, y, w, sh, _ = stats[index]
        return sh >= 0.6 * h and y <= row + 0.35 * h and y + sh >= row - 0.35 * h and start <= x + w / 2 <= end

    candidates = [index for index in range(1, count) if written(index)]
    chosen = {index for index in candidates if crosses(index)}
    reach = 0.6 * h
    grown = True
    while grown and chosen:
        grown = False
        for index in candidates:
            if index in chosen:
                continue
            x, y, w, sh, _ = stats[index]
            for other in list(chosen):
                ox, oy, ow, osh, _ = stats[other]
                if max(0, ox - (x + w), x - (ox + ow)) <= reach and max(0, oy - (y + sh), y - (oy + osh)) <= reach:
                    chosen.add(index)
                    grown = True
                    break
    keep = np.zeros(count, bool)
    keep[list(chosen)] = True
    return np.where(keep[labels], 255, 0).astype(np.uint8), row


# ---------------------------------------------------------------------------
# Comparing signatures
# ---------------------------------------------------------------------------

def body(strokes, h: int, rule_y: int):
    """The start of the signature, cut at a fixed place from its first stroke
    and the rule and scaled to a label ``REFERENCE_LABEL_HEIGHT`` high, ink
    255 on 0; None when nothing is written."""
    import cv2
    import numpy as np

    points = cv2.findNonZero(strokes)
    if points is None:
        return None
    first = cv2.boundingRect(points)[0]
    x0, x1 = int(first + BODY_ACROSS[0] * h), int(first + BODY_ACROSS[1] * h)
    y0, y1 = int(rule_y + BODY_RISE[0] * h), int(rule_y + BODY_RISE[1] * h)
    canvas = np.zeros((y1 - y0, x1 - x0), np.uint8)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(strokes.shape[1], x1), min(strokes.shape[0], y1)
    canvas[sy0 - y0: sy1 - y0, sx0 - x0: sx1 - x0] = strokes[sy0:sy1, sx0:sx1]
    size = (
        round((BODY_ACROSS[1] - BODY_ACROSS[0]) * REFERENCE_LABEL_HEIGHT),
        round((BODY_RISE[1] - BODY_RISE[0]) * REFERENCE_LABEL_HEIGHT),
    )
    return np.where(cv2.resize(canvas, size, interpolation=cv2.INTER_AREA) > 60, 255, 0).astype(np.uint8)


def body_shape(strokes, h: int, rule_y: int):
    """``body`` at ``SHAPE_SIZE``, 1 for ink; None when nothing is written."""
    cut = body(strokes, h, rule_y)
    return None if cut is None else _shape(cut)


def _shape(cut):
    import cv2
    import numpy as np

    return (cv2.resize(cut, SHAPE_SIZE, interpolation=cv2.INTER_AREA) > 40).astype(np.uint8)


def reference_shapes(signer: str) -> list:
    """``signer``'s kept signatures (``signatures/<signer>_*.png``, black on
    white), as shapes."""
    import cv2

    shapes = []
    for path in sorted(REFERENCES.glob(f"{signer.lower()}_*.png")) if signer else []:
        cut = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if cut is not None:
            shapes.append(_shape(255 - cut))
    return shapes


def _shifts(shape):
    import numpy as np

    for dx in range(-SHAPE_SHIFT, SHAPE_SHIFT + 1, 2):
        for dy in range(-SHAPE_SHIFT, SHAPE_SHIFT + 1, 2):
            yield np.roll(np.roll(shape, dy, 0), dx, 1)


def _directions(shape):
    """Where the strokes run which way: a grid of 9-bin histograms of edge
    direction, cells of about 10 x 8 px, scaled to length 1."""
    import cv2
    import numpy as np

    smooth = cv2.GaussianBlur(shape.astype(np.float32), (5, 5), 0)
    gx, gy = cv2.Sobel(smooth, cv2.CV_32F, 1, 0), cv2.Sobel(smooth, cv2.CV_32F, 0, 1)
    strength = np.hypot(gx, gy)
    direction = (np.degrees(np.arctan2(gy, gx)) % 180 // 20).astype(int) % 9
    across, down = 10, 8
    ys, xs = np.indices(shape.shape)
    cell = np.minimum(ys * down // shape.shape[0], down - 1) * across + np.minimum(xs * across // shape.shape[1], across - 1)
    vector = np.bincount((cell * 9 + direction).ravel(), weights=strength.ravel(), minlength=across * down * 9)
    return vector / (np.linalg.norm(vector) + 1e-9)


def likeness(a, b) -> float:
    """How alike two signatures' starts are, 0..1: which way their strokes
    run, where, at the best shift of one on the other.

    His seven kept signatures of 2026-10, each held to the other six, score
    0.61-0.69. The Quantity Check signatures on the same bills (another
    person's, on the same kind of line) and a vendor's seal across the rule:
    at most 0.46. How closely the strokes lie on each other was tried too and
    told them apart less well."""
    first = _directions(a)
    return max(float(first @ _directions(moved)) for moved in _shifts(b))
