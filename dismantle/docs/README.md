# Dismantling

Taking a finished good apart and putting its components back into stock — the
oil back to loose, the bottles, caps, labels and cartons back to packing
material. Mostly done to customer returns sitting in the goods-return warehouse,
but the same operation is run on plain warehouse stock, so both are supported.

> **Naming.** Users asked for the screens to say **Disassembly**, which is also
> SAP's own word (`bopotDisassembly`, "Disassembly Order"), so every label, page
> title and route in the frontend reads *Disassembly*. The app label, model
> names, API paths (`/api/v1/dismantle/…`) and Django permission codenames
> (`dismantle.can_*`) stay **dismantle** — renaming those means a data migration
> and re-running the groups command for no user-visible gain. Two words, one
> thing; do not "fix" one side to match the other.

## What this is in SAP

A **disassembly production order** (`OWOR."Type" = 'D'`), not a transfer and not
a revaluation. Verified against live Oil data, September 2026: 713 such orders
in Oil, 86 in Beverages. Oil dismantles returns out of `BH-GR`; Beverages only
dismantles out of `BH-FG` / `BH-PF`.

One dismantle is **three documents**, and their order is fixed:

| # | SAP document | Table | What it does |
|---|---|---|---|
| 1 | Disassembly production order | `OWOR` / `WOR1` | states the parent, the quantity and the components |
| 2 | Receipt from Production | `OIGN` / `IGN1` | **receives the components** |
| 3 | Issue for Production | `OIGE` / `IGE1` | **issues the parent** finished good |

Both completion documents point at the order through `BaseType 202` /
`BaseEntry`. The receipt's lines also carry `BaseLine` — SAP's own line number on
the order, read back after it is created, not the position the app sent. The
issue's line carries **no** `BaseLine`: the parent is the order's header, not one
of its component lines.

### The receipt must come before the issue

```
20206  Cannot add Goods Issue: no Goods Receipt posted for Disassembly Order N
```

From `SBO_SP_TransactionNotification`, present in all three company databases.
This reads backwards — the components come back before the thing they came out
of is consumed — and it is nevertheless what SAP enforces. The mirror rule
(`20205`) applies to *normal* production orders, where the issue comes first,
which is why this module's writer is separate from anything that completes one.

## The recipe

Component quantities are the production BOM exploded **per piece**:

```sql
ITT1."Quantity" / OITT."Qauntity"   -- OITT."Qauntity" is SAP's own typo
WHERE OITT."TreeType" = 'P' AND ITT1."Type" = 4
```

`ITT1."Type" = 4` is an inventory item; type 290 is a resource — the `JWPL…`
labour line — which lives in `ORSC` rather than `OITM` and which SAP leaves off a
disassembly order by itself (0 labour lines across 411 live orders). The same two
rules are already obeyed by `packing_material/hana_reader.py` and
`planning_purchase/hana_reader.py`.

**The inflation trap.** 24 Oil FG/FB recipes have `OITT."Qauntity" = 1` against a
`SalFactor2` greater than 1, i.e. they are written per box against a batch size
of one. The per-piece explosion then yields a box's worth of components for every
single piece. SAP's own disassembly screen divides by the same field and is wrong
in exactly the same way, so the app **warns and posts what SAP would post** rather
than silently correcting it — a corrected figure would make the app's document
disagree with SAP's, and the real fix belongs in the item master.

## Quantities are in pieces

`OWOR."PlannedQty"` and the goods issue are both in pieces, never boxes. A box
figure would dismantle a sixteenth of what was asked for.

## Batches

`FG` and `RM` items are batch-managed (`OITM."ManBtchNum" = 'Y'`); `PM` items are
not. So:

* the **issue** names the batch being consumed. For a return booked through the
  app this is already known — `goods_return` minted it when it posted, in the
  form `GR-<date>-<seq>-<line id>` — so nothing is re-keyed. Returns typed
  straight into SAP carry hand-keyed batches and are dismantled from stock.
* the **receipt** mints a fresh batch per batch-managed component
  (`<entry no>-<component id>`). It has to be new: `590001 Duplicate Batch not
  Allowed, Batch No Must be Unique` refuses a receipt into an existing one, and
  the rule is company-wide rather than per warehouse.

## Other rules this module satisfies

| Error | Rule |
|---|---|
| `60003` | every goods-issue line needs a Variety (`CostingCode`, Dimension 1) — unconditional |
| `600006` | a goods issue may not be dated ahead of today (the message calls it "Back Date", which it is not) |
| `590001` | a received batch may not already exist anywhere in the company |
| `202581` | a production order may not carry a wastage quantity (`WOR1."AdditQty"`) |

And two that deliberately do **not** apply: `590005` / `60002` ("Not Allowed to
Goods Receipt/Issue Manualy") and the GL-5100013 rules fire only on lines with
`BaseType != 202`. Because a dismantle's documents are all based on its order,
the Service Layer user may post them where it could not post a free-hand goods
movement. Likewise `2020014` and `2020041` (component count and quantity must
match the BOM) fire only on `Type = 'S'` orders, which is why an operator may
leave a component off a dismantle.

## What the app adds that SAP cannot hold

SAP's disassembly order has no link back to the return: `OriginAbs`, `OriginNum`
and `Comments` are null on all 411 live orders, and `CardCode` is the same dummy
vendor throughout. The `Dismantle` record is therefore the only place
"which return did this stock come from" is answerable. The three documents also
carry it in their `Comments` text, so it is at least legible to someone reading
them inside SAP.

## Half-posted runs

SAP can take documents 1 and 2 and refuse 3, and the app can withdraw none of
them. Such a run is kept, not rolled back: the record goes to
`PARTIALLY_POSTED`, each document it got is recorded, and posting again resumes
at the first document SAP does not have. Only a run where *nothing* reached SAP
raises and rolls back.

Closing the order (`Status 'L'`) is cosmetic — the stock has already moved — so a
dismantle whose close failed is still `POSTED`, with the reason on
`sap_post_error`.

## Deploying

```
python manage.py migrate dismantle
python manage.py setup_dismantle_groups
```

Then put people in **Dismantle Operator** (prepare) and **Dismantle Poster**
(commit to SAP). The split is deliberate: posting is irreversible from here.

## Not in scope

Good-condition returns are **not** dismantled — they leave the returns warehouse
by inventory transfer (`BaseType 67`). That disposition is somebody else's
screen; this module only takes things apart.
