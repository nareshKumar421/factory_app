# Short Dispatch

The warehouse's own return note, for stock a posted bill says went out but which
never left the floor.

## Why it exists

A bill summary posted into SAP is read as the stock having gone: the invoice is
raised, inventory is relieved, and as far as SAP is concerned the goods are on the
truck. When a line is then found **short** — the pallet was not there, the loader
could not fit it, the item was pulled off the load — the goods are still standing
in the warehouse while SAP says they are not. SAP users fix this by posting a
Return Note. This module is that, done from the app.

## Why it is not a basis on `goods_return`

It posts the same SAP document (A/R Return, object ORDN), and nothing else about
it is the same:

| | Customer return | Short dispatch |
|---|---|---|
| Vehicle / driver | Required — it is a truck arriving | None. Nothing travels |
| Gate | Joins the "Goods Return In" queue, marked in by the gate | No gate at all |
| Approval | Can arrive "on approval", waits for an admin | None |
| Basis | Invoice, debit note or letter pad | Always one invoice |
| Destination | A `-GR` warehouse, for damaged goods to be looked at | The warehouse it was billed out of — it never moved |
| Shape | Three-page wizard, draft → gate-in → receive | One form, posted on submit |

## The flow

1. Type the SAP invoice number. `GET /api/v1/short-dispatch/invoice/` returns its
   lines with three things added: the batch SAP actually issued on each line
   (IBT1), how much of the line an earlier short dispatch already returned, and
   the quantity left.
2. Enter the short quantity and a reason per line, and confirm the warehouse (it
   is preselected to the floor most of the bill was picked from).
3. Submit. `POST /api/v1/short-dispatch/` writes the record **and** posts the A/R
   Return in one transaction.

There is no draft stage. Either SAP takes the document and the entry exists, or
SAP refuses it, the transaction rolls back, and the operator is told why with the
form still in front of them — a half-saved short dispatch would be a record of a
correction nobody made.

## Endpoints

| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/v1/short-dispatch/` | `can_view_short_dispatch` |
| `POST` | `/api/v1/short-dispatch/` | `can_create_short_dispatch` |
| `GET` | `/api/v1/short-dispatch/<id>/` | `can_view_short_dispatch` |
| `GET` | `/api/v1/short-dispatch/<id>/print/` | `can_view_short_dispatch` |
| `GET` | `/api/v1/short-dispatch/invoice/?invoice_number=` | `can_create_short_dispatch` |
| `GET` | `/api/v1/short-dispatch/warehouses/` | `can_create_short_dispatch` |

Creating *is* posting, so `can_create_short_dispatch` is a permission to change
stock in SAP, not merely to fill a form in.

## Things worth knowing

**The SAP rules are imported, not copied.** `goods_return/guards.py` holds every
rule an A/R Return must satisfy — current-month dating, no returns to an internal
branch, an upper-case reference, a Variety and a tax code and a return cost on
every line, the GST flavour matching the place of supply. They belong to the
document, not to the module posting it, so this module imports them. A rule
change lands in one place.

**The batch necessarily changes.** SAP refuses a return into a batch that already
exists (`10001226`), even one held in another warehouse, so the posted document
mints a fresh number (`<entry no>-<line id>`). For a short dispatch this is a real
divergence: the pallet on the floor still carries its original batch while SAP now
holds the stock under a new one. The billed batch is read from IBT1, stored on the
line, and written into the SAP line's free text — the only places it survives.

**The customer is not credited here.** Lines post at zero price: the stock comes
back, but the credit note against the invoice is finance's own separate document.

**One bill can be short twice.** A second entry against the same invoice is
allowed — a further shortfall can be found later — but between them they cannot
return more than was billed. SAP would not catch that; `_prepare_lines` does.

**A posted return cannot be withdrawn from here.** SAP restricts cancelling an A/R
Return to a named list of users and the app's Service Layer user is not among them
(a live `Cancel` came back `-1116`). Everything checkable is therefore checked
before SAP is called, and SAP is called last, after every database write.

## Deploy

```
python manage.py migrate short_dispatch
python manage.py setup_short_dispatch_groups
```

Then assign **Short Dispatch Operator** to whoever posts them and **Short Dispatch
Viewer** to everyone else who needs to see what came back off a bill.
