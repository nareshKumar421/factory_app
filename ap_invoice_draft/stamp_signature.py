"""The signature in the gate's receiving stamp, matched by its shape.

Every bill that reaches the store carries the gate's rubber stamp: "JIVO
WELLNESS PVT. LTD.", the village line, then G. No. / Item / V.No. / Time on
the left and the date / Qty / Signature on the right. Kulbeer signs its
Signature box with the same short mark every time ("131" with a looped tail
to OCR). It is a mark, not letters, so it is matched by its shape against
marks of his kept in ``signatures/`` rather than read.

The stamp is found by its heading, straightened (it goes on by hand, at an
angle), and the mark is cut from just after its printed word "Signature",
which is found by its shape. The stamp's borders are no guide: where it
lands on the bill's table they break up.
"""

import re
from pathlib import Path

#: The stamp's heading, and the village line printed under it, which tells
#: it from the buyer's name printed on the bill.
STAMP_HEADING = re.compile(r"jiv\w*\s*wel", re.I)
STAMP_VILLAGE = re.compile(r"\bvill?ag", re.I)

#: Kulbeer's marks, and the stamp's printed word "Signature".
REFERENCES = Path(__file__).resolve().parent / "signatures"
WORD_TEMPLATE = REFERENCES / "stamp_word_signature.png"

#: Where the printed "Signature" is looked for on the straightened stamp, in
#: widths of its heading (W) from the heading's top left, (across, down). On
#: six stamps it sat 0.52-0.82 across and 0.60-0.72 down.
WORD_AREA = ((0.35, 1.05), (0.45, 0.85))
#: The mark, from the word: starting this far before the word's end (a mark
#: may start over its last letters), this wide, and this high above its foot.
MARK_FROM_WORD_END = 0.04
MARK_WIDTH = 0.24
MARK_HEIGHT = 0.17
#: Ink wholly in the window's bottom strip is the dotted line or the bill's own
#: print showing under the stamp, not the mark, which is written above it.
BOTTOM_STRIP = 0.18


def read_stamp_signature(image, page_lines: list[dict], signer: str) -> dict:
    """The stamp's Signature box on this page: ``found`` (the stamp and its
    borders), ``signed`` (anything written in it), and ``match``, how alike
    the mark is to ``signer``'s kept marks, 0..1 (None with none kept)."""
    for heading in stamp_headings(page_lines):
        stamp, w, heading_at = straightened_stamp(image, heading)
        window = mark_window(stamp, w, heading_at)
        if window is not None:
            break
    else:
        return {"found": False}
    mark = signature_mark(window[0], w)
    if mark is None:
        return {"found": True, "signed": False, "signer": signer, "match": None}
    kept = reference_shapes(signer)
    shape = mark_shape(mark)
    match = max((likeness(shape, other) for other in kept), default=None)
    return {
        "found": True, "signed": True, "signer": signer,
        "match": None if match is None else round(match, 3),
    }


def reference_shapes(signer: str) -> list:
    """``signer``'s kept marks (``signatures/<signer>_*.png``), as shapes."""
    import cv2

    shapes = []
    for path in sorted(REFERENCES.glob(f"{signer.lower()}_*.png")) if signer else []:
        mark = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if mark is not None:
            shapes.append(mark_shape(255 - mark))
    return shapes


def stamp_headings(page_lines: list[dict]) -> list[dict]:
    """The lines that may be the stamp's heading, as OCR read them.

    The heading has the stamp's village line just under it, starting under
    its left edge. OCR may read the bill's own print into that line where the
    stamp lands on it ("IGST Village Bhakharpur..."); a buyer's address has the
    word further along ("20/9/2 & 10/1/2, VILLAGE BAKHARPUR").
    """
    headings = []
    for line in page_lines:
        if not STAMP_HEADING.search(line["text"]):
            continue
        x0, y0, x1, y1 = line["box"]
        h = y1 - y0
        for other in page_lines:
            village = STAMP_VILLAGE.search(other["text"])
            if village is None or not y0 + 0.3 * h <= other["box"][1] <= y1 + 1.5 * h:
                continue
            ox0, _, ox1, _ = other["box"]
            starts_at = ox0 + (ox1 - ox0) * village.start() / max(1, len(other["text"]))
            if abs(starts_at - x0) <= 0.15 * (x1 - x0):
                headings.append(line)
                break
    return headings


def _ink(image, threshold: int = 150):
    import cv2
    import numpy as np

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return (gray < threshold).astype(np.uint8) * 255


def straightened_stamp(image, heading: dict):
    """``(the stamp cut out and turned level, W, the heading's top left in it)``."""
    import cv2
    import numpy as np

    x0, y0, x1, _ = heading["box"]
    w = x1 - x0
    left, top = max(0, x0 - int(0.1 * w)), max(0, y0 - int(0.15 * w))
    crop = image[top: min(image.shape[0], y0 + int(1.0 * w)), left: min(image.shape[1], x0 + int(1.2 * w))]
    ink = _ink(crop)
    found = cv2.HoughLinesP(ink, 1, np.pi / 720, threshold=80, minLineLength=int(0.4 * w), maxLineGap=6)
    angles = [
        np.degrees(np.arctan2(y2 - ya, x2 - xa))
        for xa, ya, x2, y2 in (found.reshape(-1, 4) if found is not None else [])
        if abs(np.degrees(np.arctan2(y2 - ya, x2 - xa))) < 8
    ]
    angle = float(np.median(angles)) if angles else 0.0
    turn = cv2.getRotationMatrix2D((crop.shape[1] / 2, crop.shape[0] / 2), angle, 1.0)
    level = cv2.warpAffine(crop, turn, (crop.shape[1], crop.shape[0]), borderValue=(255, 255, 255))
    hx, hy = turn @ np.array([x0 - left, y0 - top, 1.0])
    return level, w, (int(hx), int(hy))


def mark_window(stamp, w: int, heading_at: tuple[int, int]):
    """The Signature box from just before the printed word's end, with the
    word painted out: ``(image, (x, y))``, or None if the word is not found."""
    import cv2
    import numpy as np

    found = _printed_word(_ink(stamp), w, heading_at)
    if found is None:
        return None
    (wx, wy), word = found
    stamp = stamp.copy()
    printed = cv2.dilate(word, np.ones((3, 3), np.uint8)) > 0
    stamp[wy: wy + word.shape[0], wx: wx + word.shape[1]][printed] = 255
    end, foot = wx + word.shape[1], min(stamp.shape[0], wy + word.shape[0] + max(2, int(0.005 * w)))
    left = max(0, end - int(MARK_FROM_WORD_END * w))
    right = min(stamp.shape[1], end + int((MARK_WIDTH - MARK_FROM_WORD_END) * w))
    top = max(0, foot - int(MARK_HEIGHT * w))
    return stamp[top: foot, left: right], (left, top)


def _printed_word(ink, w: int, heading_at: tuple[int, int]):
    """Where the stamp's printed "Signature" is: ``((x, y), its ink at that
    size)``, or None if the template does not find it."""
    import cv2
    import numpy as np

    template = cv2.imread(str(WORD_TEMPLATE), cv2.IMREAD_GRAYSCALE)
    if template is None:
        return None
    hx, hy = heading_at
    (across_from, across_to), (down_from, down_to) = WORD_AREA
    left, top = max(0, hx + int(across_from * w)), max(0, hy + int(down_from * w))
    right, bottom = min(ink.shape[1], hx + int(across_to * w)), min(ink.shape[0], hy + int(down_to * w))
    search = cv2.GaussianBlur(ink[top: bottom, left: right], (5, 5), 0).astype(np.float32)
    word = (template < 128).astype(np.uint8) * 255
    best = (0.3, None)
    for scale in np.linspace(0.85, 1.15, 13):
        # The template was cut from a stamp whose heading is 409 px wide.
        sized = cv2.resize(word, None, fx=scale * w / 409, fy=scale * w / 409, interpolation=cv2.INTER_NEAREST)
        if sized.shape[0] >= search.shape[0] or sized.shape[1] >= search.shape[1]:
            continue
        found = cv2.matchTemplate(search, cv2.GaussianBlur(sized, (5, 5), 0).astype(np.float32), cv2.TM_CCOEFF_NORMED)
        _, score, _, at = cv2.minMaxLoc(found)
        if score > best[0]:
            best = (score, ((left + at[0], top + at[1]), sized))
    return best[1]


def signature_mark(window, w: int):
    """The handwriting in the window, ink 255 on 0 and cut close, or None when
    there is none. The stamp's borders and the bill's ruled lines (as long as
    the window is high), specks, and what lies wholly in the bottom strip are
    left out. Colour does not help: a thin pen stroke is barely bluer than
    black print under the stamp's ink."""
    import cv2
    import numpy as np

    ink = _ink(window)
    for kernel in (
        cv2.getStructuringElement(cv2.MORPH_RECT, (int(0.15 * w), 1)),
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.9 * window.shape[0])))),
    ):
        rules = cv2.dilate(cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel), np.ones((3, 3), np.uint8))
        ink = cv2.subtract(ink, rules)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink)
    keep = np.zeros(count, bool)
    strip = (1 - BOTTOM_STRIP) * window.shape[0]
    for index in range(1, count):
        x, y, cw, ch, area = stats[index]
        keep[index] = max(cw, ch) >= 0.03 * w and area >= 0.00015 * w * w and y < strip
    mark = np.where(keep[labels], 255, 0).astype(np.uint8)
    points = cv2.findNonZero(mark)
    if points is None:
        return None
    x, y, cw, ch = cv2.boundingRect(points)
    if ch < 0.05 * w:
        return None
    return mark[y: y + ch, x: x + cw]


# ---------------------------------------------------------------------------
# Comparing marks
# ---------------------------------------------------------------------------

#: Marks are compared at this size, fitted into the square with its shape kept.
SHAPE_SIZE = 64
#: How far one mark may be shifted on the other, in pixels of that square.
SHAPE_SHIFT = 4


def mark_shape(mark):
    """The mark fitted into a ``SHAPE_SIZE`` square, 1 for ink."""
    import cv2
    import numpy as np

    h, w = mark.shape
    scale = (SHAPE_SIZE - 2 * SHAPE_SHIFT) / max(h, w)
    small = cv2.resize(mark, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    shape = np.zeros((SHAPE_SIZE, SHAPE_SIZE), np.uint8)
    y, x = (SHAPE_SIZE - small.shape[0]) // 2, (SHAPE_SIZE - small.shape[1]) // 2
    shape[y: y + small.shape[0], x: x + small.shape[1]] = small > 64
    return shape


def _shifts(shape):
    import numpy as np

    for dx in range(-SHAPE_SHIFT, SHAPE_SHIFT + 1, 2):
        for dy in range(-SHAPE_SHIFT, SHAPE_SHIFT + 1, 2):
            yield np.roll(np.roll(shape, dy, 0), dx, 1)


def stroke_overlap(a, b) -> float:
    """How closely the strokes of two shapes lie on each other, 0..1: the mean
    distance from each one's ink to the other's, both ways, best shift."""
    import cv2
    import numpy as np

    to_a = cv2.distanceTransform((1 - a).astype(np.uint8), cv2.DIST_L2, 3)
    best = 0.0
    for moved in _shifts(b):
        to_b = cv2.distanceTransform((1 - moved).astype(np.uint8), cv2.DIST_L2, 3)
        distance = (to_b[a > 0].mean() + to_a[moved > 0].mean()) / 2
        best = max(best, float(np.exp(-distance / 3)))
    return best


def _directions(shape):
    """Where the strokes run which way: an 8 x 8 grid of 9-bin histograms of
    edge direction, scaled to length 1."""
    import cv2
    import numpy as np

    smooth = cv2.GaussianBlur(shape.astype(np.float32), (5, 5), 0)
    gx, gy = cv2.Sobel(smooth, cv2.CV_32F, 1, 0), cv2.Sobel(smooth, cv2.CV_32F, 0, 1)
    strength = np.hypot(gx, gy)
    direction = (np.degrees(np.arctan2(gy, gx)) % 180 // 20).astype(int) % 9
    cell = SHAPE_SIZE // 8
    ys, xs = np.indices(shape.shape)
    index = ((ys // cell) * 8 + xs // cell) * 9 + direction
    vector = np.bincount(index.ravel(), weights=strength.ravel(), minlength=8 * 8 * 9)
    return vector / (np.linalg.norm(vector) + 1e-9)


def stroke_directions(a, b) -> float:
    """How alike the run of the strokes is, 0..1, best shift."""
    first = _directions(a)
    return max(float(first @ _directions(moved)) for moved in _shifts(b))


def likeness(a, b) -> float:
    """How alike two marks are, 0..1: where the strokes lie and which way they
    run, averaged. Kulbeer's six marks of 2026-10 score 0.69-0.75 against
    each other; 65 other pieces of handwriting on the same bills (dates, qty,
    vehicle and gate numbers) at most 0.57."""
    return (stroke_overlap(a, b) + stroke_directions(a, b)) / 2
