"""
Every SAP fact this module rests on, in one place, with the evidence.

WHAT COUNTS AS A LABEL OR A CARTON
----------------------------------
``OITM.ItmsGrpCod`` = 105 ('PACKAGING MATERIAL' in ``OITB``) narrowed by the
user-defined field ``OITM.U_Sub_Group``, which SAP populates with the packaging
kind: LABEL, CARTON, CAPS, SHRINK, PREFORM, POUCH, TIKKI and so on. Only LABEL
and CARTON are artwork-bearing, so only those two are offered here.

An item-code prefix is never matched on. ``PM`` covers the whole packaging
group, and the sub-group is what SAP actually classifies by.

Verified against the three live schemas on 14 September 2026 -- items with
``validFor = 'Y'`` in group 105:

    JIVO_OIL        LABEL 500   CARTON  93      (593 artwork items)
    JIVO_BEVERAGES  LABEL 125   CARTON  46      (171 artwork items)
    JIVO_MART       LABEL   0   CARTON  33      ( 33 artwork items)

Mart carrying no LABEL items at all is real, not a query fault: Mart is a
trading company and its packaging master is cartons only. A Mart user filtering
to labels correctly sees an empty list.

Both filters are needed together. ``U_Sub_Group`` is free text on the item
master and a handful of FIXED ASSETS rows (group 112) hold a machine name that
contains the word 'LABELING'; the group check keeps them out.

WHY THE BARCODE IS CAPTURED HERE AND NOT READ FROM SAP
------------------------------------------------------
``OITM.CodeBars`` is null for all 593 Oil artwork items -- SAP holds no barcode
for a label or a carton, because the barcode belongs to the finished good the
label goes on. The number printed on the artwork therefore exists nowhere in
SAP and has to be recorded by whoever is holding the artwork.
"""

#: SAP item group for packaging material. The same code in all three schemas.
PACKAGING_ITEM_GROUP_CODE = 105

#: The artwork-bearing packaging kinds, as spelled in ``OITM.U_Sub_Group``.
SUB_GROUP_LABEL = "LABEL"
SUB_GROUP_CARTON = "CARTON"
ARTWORK_SUB_GROUPS = (SUB_GROUP_LABEL, SUB_GROUP_CARTON)

#: Upload limits. Artwork PDFs are print-ready and CorelDRAW sources carry
#: embedded images, so both run far larger than a scanned QC sheet.
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_CDR_BYTES = 100 * 1024 * 1024

PDF_EXTENSIONS = {".pdf"}
PDF_CONTENT_TYPES = {"application/pdf"}

#: CorelDRAW. Browsers have no registered type for it and send
#: ``application/octet-stream`` (or nothing at all), so the extension is what
#: decides -- see ``services.validate_upload``.
CDR_EXTENSIONS = {".cdr"}
CDR_CONTENT_TYPES = {
    "application/cdr",
    "application/x-cdr",
    "image/x-cdr",
    "application/coreldraw",
    "application/octet-stream",
}
