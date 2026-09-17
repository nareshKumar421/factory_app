"""
The spreadsheet a bunch is sent to head office as.

This is the deliverable, not a convenience: the batch exists to be mailed, and
what gets mailed is this file. So it is shaped like the sheet Delhi already
reads -- the workbook's own "Send" tab, which prints each batch as a short
table of vouchers under a Total.

That tab's columns were ``Voucher no. | Date | Details | Amount | Unit``. Two
are kept as they were, two are said more plainly, and one is added:

* **Voucher no.** is the entry's id here. The old sheet used its row number,
  which meant a voucher could not be found again once the sheet was re-sorted;
  an id is the same number whoever looks and whenever.
* **Unit** is the branch, which is what it always held.
* **G/L head** is new. The old sheet left it on the other tab, so anybody
  querying a line had to go and find it.
"""

from io import BytesIO

from django.utils import timezone

#: Widths that fit the real data: a detail line runs long, an amount does not.
COLUMN_WIDTHS = [12, 12, 16, 30, 18, 52, 14]

HEADERS = [
    "Voucher no.",
    "Date",
    "Branch",
    "G/L code",
    "G/L head",
    "Details",
    "Amount",
]


def build_bunch_workbook(bunch, entries):
    """One batch as an ``.xlsx``, returned as bytes ready to be served.

    ``entries`` is passed in rather than read off the bunch so the caller
    decides the order and the filtering, and so this stays a pure function of
    what it is given -- easy to check, and it cannot surprise anybody by
    quietly including a cancelled voucher.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, Side

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = f"Bunch {bunch.number}"

    bold = Font(bold=True)
    thin = Side(style="thin")
    boxed = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = "#,##0.00"

    # --- the heading, so the file says what it is once it is off the screen
    sheet.append([f"Cash voucher batch {bunch.number}"])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([f"{bunch.company.name} - cash book"])
    sheet.append(
        [
            "Prepared",
            timezone.localtime(bunch.created_at).strftime("%d-%m-%Y %H:%M"),
            "Sent",
            timezone.localtime(bunch.sent_at).strftime("%d-%m-%Y %H:%M")
            if bunch.sent_at
            else "not yet",
        ]
    )
    if bunch.remarks:
        sheet.append(["Remarks", bunch.remarks])
    sheet.append([])

    header_row = sheet.max_row + 1
    sheet.append(HEADERS)
    for cell in sheet[header_row]:
        cell.font = bold
        cell.border = boxed
        cell.alignment = Alignment(horizontal="center")

    total = 0
    for entry in entries:
        sheet.append(
            [
                entry.id,
                entry.entry_date.strftime("%d-%m-%Y"),
                entry.branch.name if entry.branch else "",
                entry.gl_account_code,
                entry.gl_account_name,
                entry.detail,
                float(entry.amount),
            ]
        )
        total += entry.amount
        for cell in sheet[sheet.max_row]:
            cell.border = boxed
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(row=sheet.max_row, column=7).number_format = money

    # --- the Total, which is the figure the old sheet used as the batch's name
    sheet.append([])
    total_row = sheet.max_row + 1
    sheet.append(["", "", "", "", "", "Total", float(total)])
    for column in (6, 7):
        cell = sheet.cell(row=total_row, column=column)
        cell.font = bold
        cell.border = boxed
    sheet.cell(row=total_row, column=7).number_format = money

    for index, width in enumerate(COLUMN_WIDTHS, start=1):
        sheet.column_dimensions[
            openpyxl.utils.get_column_letter(index)
        ].width = width

    # The header stays put when Delhi scrolls a fifty-voucher batch.
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def bunch_filename(bunch) -> str:
    """What the file should be called once it is sitting in somebody's inbox."""
    company = (bunch.company.code or "cash").lower().replace("_", "-")
    return f"{company}-bunch-{bunch.number}.xlsx"
