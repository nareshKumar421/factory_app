# Smart Supply Chain System — what the brief is asking for

Reading of two documents circulated in August 2026 by Kamalpreet Kaur (Software
Development):

- **JIVO Supply Chain Management Brief.docx** — the management pitch.
- **JIVO Supply Chain Reference Template.xlsx** — the data-collection workbook
  already prepared for circulation to departments.

This file restates them in engineering terms, separates what is actually
specified from what only sounds specified, and lists what already exists in this
repository. It is a reading of the brief, not a design or a commitment.

---

## 1. The ask in one paragraph

Five departments — packaging procurement, oil/raw-material procurement, finance,
infrastructure (storage and filling-line capacity), and production — each run
their own numbers in their own sheets. The brief asks for one system that pulls
the shared facts out of SAP automatically, takes three pieces of reference data
the ERP does not hold, and turns the monthly plan into a **time-phased,
department-wise action list**: what to produce, what to buy, and *when to place
each order* so nothing stops the line. The stated goal is not another dashboard
but an **alarm** — the system should say "order this today" before a shortfall
becomes visible as a stopped line.

## 2. The problem as stated

| Stated problem | What it means for a build |
| --- | --- |
| Siloed information | One shared read model, not five sheets |
| Reactive, not proactive | Lead-time-aware alarms, not status displays |
| HOD overload | The reconciliation itself must be computed, not presented |
| No early warning | Requirements must be dated, not just totalled |
| Inconsistent buffers | The 35% floor must be enforced by the system, not by habit |

The success criterion given is organisational: **one HOD plus a team of up to
five** steering plan-to-dispatch, replacing five HODs reconciling manually.

## 3. The core chain

This is the substance of the brief. One pass, per SKU:

```
1. Demand signal      monthly plan  +  last 3 months actual sales
2. Stock floor        35% of the 3-month sales trend  (FG and PM both)
3. FG gap             demand + floor − FG stock on hand   →  produce this
4. BOM explosion      FG (parent) → children (packaging + raw), live BOM
5. Material need      exploded need − PM stock − PM floor →  procure this
6. Timing             apply per-material lead time        →  order now / later
7. Feasibility        machine capacity + material→machine →  is it achievable
```

Steps 1–5 are arithmetic over SAP data. Step 6 is what makes it an alarm system
rather than a report. Step 7 is the reality check that stops the plan being
accepted on paper when no line can actually run it.

## 4. Where the data comes from

The brief is deliberate that departments are asked only for what the ERP cannot
supply. That split is the main design constraint.

**Pulled automatically (no human input):**

- Sales history, last 3 months
- Minimum stock levels — *derived* from that history, not entered
- Live stock on hand
- Bill of Materials

**Supplied by departments, via the template:**

| Reference data | Owner |
| --- | --- |
| Lead times + MOQ, per purchased material | Procurement (Packaging) + Procurement (Oils/RM) |
| Machine capacities | Production / Infrastructure |
| Material-to-machine mapping | Production |

**Supplied per cycle:** the monthly production plan, from Planning.

## 5. The reference template

Three data sheets behind a README. Blue cells are input; grey italic rows are
worked examples using real codes and must not be ingested as data.

**1. Lead Times** — one row per purchased material.
`Material Code · Material Name · Material Type (Packaging | Oil/Raw Material) ·
Category/Spec · Supplier Name · Lead Time (Days) · MOQ · Unit · Remarks`
Lead time is defined in the sheet as *order placed → material usable in
production*, which is wider than supplier transit and is the right definition for
step 6.

**2. Machine Capacities** — one row per filling line.
`Machine ID · Name · Location · Pack Type · Pack Size Range · Output (Units/Hour)
· Shift Hours · Shifts/Day · Working Days/Month · Effective Monthly Capacity ·
Changeover (Min)`
Effective monthly capacity is a formula in the sheet
(`Output × Shift Hours × Shifts/Day × Working Days`), not an entered number.
Example lines: M-01 PET, M-02 JAR, M-03 TIN, M-04 POUCH.

**3. Material-to-Machine Map** — which SKU runs where.
`SKU Code · FG Name · Brand · Pack Type · Pack Size · Primary Machine ID ·
Alternate Machine ID(s) · Output on Primary (Units/Hr)`
Alternates are comma-separated. Example SKUs are real: FG0000030, FG0000142,
FG0000011, FG0000316.

## 6. What the brief does not settle

These are not gaps in the writing — they are decisions that were never in scope
for a management brief, and each one changes the numbers materially. They need
answers from Planning/Procurement before any of this is built.

1. **The 35% floor has no base.** "35% of the three-month sales trend" could mean
   35% of the three-month *total*, or 35% of the *monthly average*. Those differ
   by 3×. Everything downstream inherits the choice.

2. **Step 5 contradicts step 3 on the floor's sign.** Step 3 compares stock
   against "demand **plus** the 35% floor" — the floor is demand you must cover.
   Step 5 says to "subtract packaging stock on hand **and** its own 35% floor" —
   which subtracts the floor instead. One of the two is wrong; on the reading
   that makes the floor a genuine buffer, both should be
   `requirement = need + floor − on_hand`.

3. **"Combine plan with actual sales" is undefined.** Whether demand is the plan,
   the trend, the greater of the two, or a weighting is the single largest driver
   of what gets produced.

4. **Open purchase orders and in-transit stock are never mentioned.** Without
   netting them off, step 5 re-orders material that is already on order — every
   cycle, until it arrives. This is the most likely way a first version produces
   confidently wrong alarms.

5. **Work in progress and already-scheduled production** have the same problem at
   step 3.

6. **Which stock counts** — total on hand, or free (uncommitted) stock, and across
   which warehouses. Marketplace and factory stock sit in different godowns.

7. **MOQ is collected but never used.** Presumably requirements round up to MOQ;
   the brief does not say so.

8. **Changeover minutes are collected but absent from step 7.** A capacity check
   that ignores changeover will pass plans the floor cannot run.

9. **No rule for choosing among alternate machines** when the primary is full.

10. **Alarms have no addressee.** Who is notified, through which channel, at what
    threshold, and what acknowledgement looks like are all unstated.

11. **Company scope** — JIVO_OIL, JIVO_MART, or both.

## 7. What already exists here

A meaningful part of steps 1–5 is not greenfield.

| Need | Existing code |
| --- | --- |
| Forecast → BOM-driven component requirement | `sales_planning_requirement/` — already calls a HANA procedure per forecast and stores per-item `planned_qty` rows (`SalesPlanningRequirementRow`) |
| BOM resolution for an item | `production_execution/services/bom_utils.py`, `production_execution/services/sap_reader.py` |
| Stock on hand (OITW) | `warehouse/services/wms_hana_reader.py`; `marketplace` uses `oitw_onhand` for its stock partition |
| Sales/invoice history | `dispatch_plans/hana_reader.py` |
| Stock-ageing precedent | `non_moving_rm/` |
| HANA/SAP connection layer | `sap_client/` |
| Alarm delivery | `notifications/` |

`sales_planning_requirement` overlaps steps 1 and 4 most directly and is the
first thing to read before designing anything new — the question is whether this
system extends it or sits beside it.

## 8. What the brief asks for next

Three approvals, not a build instruction:

1. Department sign-off to return the reference template.
2. Approved ERP access for sales, stock and BOM.
3. Confirmation of the owning HOD and a team of up to five.

The stated next step is a **working dashboard built and demonstrated for
review** — a demo to secure the reference data and the mandate, not a production
rollout.
