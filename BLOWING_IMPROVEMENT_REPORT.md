# Blowing Module — Improvement & Make‑vs‑Buy Report

*Turning the Linear.xlsx spreadsheet into a decision tool that answers one question:
**is it cheaper to blow bottles in‑house, or to buy them?***

Prepared for the FactoryFlow / factory_app blowing module. Research‑backed; sources cited
inline and listed at the end. All ₹ figures use the shop's own Linear.xlsx numbers.

---

## 1. Executive summary

The blowing module today is a faithful software copy of the Excel sheet: it records daily
runs and computes a **conversion cost per bottle** (blowing + packing ≈ ₹0.675 for a 40 g
bottle). That is useful operationally, but it **cannot answer the question it was built for.**
Two things are missing:

1. **The full "make" cost.** The conversion cost the sheet obsesses over is **less than 10 %
   of the true cost of a bottle.** For a 40 g bottle the preform/resin alone is ≈ ₹6.3, versus
   ≈ ₹0.68 of blowing+packing. Any make‑vs‑buy answer is dominated by material, plus costs the
   sheet ignores entirely: **mould amortization, machine depreciation, factory overhead, and QA.**
   Fixed costs also mean the per‑bottle cost **rises when the machine runs below capacity**
   (fixed‑cost absorption) — a run‑by‑run number hides this.

2. **The "buy" side.** There is no purchase price, no landed‑cost, no supplier. Without it there
   is nothing to compare against.

**The real decision is narrow and answerable:** you already *buy* the preform (a supplier turns
resin into a preform). "Make" = **buy preform + blow it here**; "Buy" = **buy the finished blown
bottle**. So the comparison reduces to:

> **Your blowing conversion + amortization + overhead + wastage**  ⟷  **the supplier's blowing
> margin baked into the finished‑bottle price (plus your inbound freight & carrying cost).**

This report benchmarks what leading manufacturing software does, maps the gaps, and specifies
(a) a corrected **fully‑loaded cost model**, (b) a **make‑vs‑buy decision model** with breakeven
and sensitivity, and (c) the **UX** to make operators and managers enjoy using it.

**Top recommendations (detail in §5–§8):**

| # | Recommendation | Why it matters |
|---|----------------|----------------|
| R1 | Add the missing cost components: **preform/resin cost on the run, mould amortization, machine depreciation, factory overhead, QA/rework** | Without them "make cost" is understated by ~90 % and the decision is wrong |
| R2 | Add a **Buy side**: finished‑bottle purchase price + freight + carrying, per SKU/size | You cannot decide make‑vs‑buy with only the make side |
| R3 | Build a **Make‑vs‑Buy dashboard**: per‑bottle make vs buy, **breakeven volume**, monthly verdict, savings run‑rate | This is the deliverable's entire purpose |
| R4 | Split **fixed vs variable** cost and show **cost‑vs‑capacity** — per‑bottle cost at 100 % vs actual utilization | Make looks cheap at full load, expensive when idle; the decision must see this |
| R5 | Add **standard vs actual + variance analysis** (material/energy/labour/yield) | Turns the tool from record‑keeping into cost control (industry standard) |
| R6 | **UX overhaul**: fast shop‑floor entry, live cost preview, KPI cards, trend & Pareto charts, alerts | "Feel‑good" adoption; matches MES best practice |
| R7 | **Keep** and promote per‑machine **energy‑to‑cost** tracking | A genuine edge — leading MES dashboards *don't* have it out of the box |

> ⚠️ **Process correction (important):** this shop runs **PET stretch blow molding**
> (preform → bottle), **not extrusion blow molding**. Common blow‑molding cost formulas that
> multiply "parison weight ÷ material‑utilization (50–90 %)" describe *extrusion* molding, which
> produces parison flash and pinch‑off scrap. Stretch blow molding has **no parison and no flash**;
> material yield ≈ **100 % − reject %**, and rejects are whole scrapped preforms/bottles. This
> means the module's current wastage model (`rejection_pcs × preform weight × preform rate`) is
> **correct for this process** — do **not** import extrusion "utilization" formulas. (Verified
> against DFMA blow‑molding docs and stretch‑vs‑extrusion sources; see §5.)

---

## 2. Where the module is today

**Captured per run (one machine + preform size + date):** preform boxes used, electricity meter
start/stop → machine units, utility units → total units, total counter production, rejection pcs,
operator/contract/own labour counts, carton scrap value. **Computed:** operator cost, labour cost,
electricity cost, wastage cost, scrap recovery, net cost, blowing cost/bottle, + fixed packing =
**per‑bottle cost.** Rates are governed in a rate config and snapshotted onto each run.

**What that gives us:** an accurate **daily conversion‑cost** ledger — better than Excel because
it is multi‑company, permissioned, validated, and reportable. **What it does not give us:** the
full landed cost of a made bottle, the purchase price of a bought bottle, the effect of volume on
cost, or any decision output. The spreadsheet's own hidden `Sheet1` already gropes toward this —
it lists, per preform size, resin cost/bottle + packing = a total (e.g. 40 g → ≈ ₹6.24 material +
₹0.5 = ₹6.74) — confirming the make‑vs‑buy intent was always there but never completed.

---

## 3. What leading manufacturing software provides (benchmark)

Findings below are from the research pass; product‑specific items are verified against vendor
primary docs, methodology items against accounting authorities (OpenStax, CFI, ACCA, ISM).

### 3.1 MES / production tracking (MachineMetrics, Tulip, Plex, Katana, SAP DM)
- **"Core Four" MES features**: Downtime Tracking, Work‑Order Management, Production Scheduling,
  OEE Analytics (MachineMetrics support docs).
- **KPIs that are considered table‑stakes**: **OEE** (= Availability × Performance × Quality),
  **Utilization %** (in‑cycle time), **Parts Goal %** (produced / target / remaining), **Cycle
  Time** (actual vs expected), **Scrap Rate**, **Tool Life %**, and **Downtime** broken down by
  cause (MachineMetrics dashboards guide — 19 widgets documented).
- **Downtime Pareto** (bars + cumulative line, 80/20) with categorized causes — Setup/Changeover,
  Waiting for Material, Tooling, Maintenance, Quality — is a standard, expected visualization.
- **Operator interface** ("Empower Your Operators"): real‑time frontline screens to *clock in,
  track jobs, **log scrap with reason codes**, run manual processes, submit labour tickets* — from
  one unified touch/tablet screen. This is the shop‑floor data‑entry pattern across MES vendors.
- **Notable gap we can exploit:** MachineMetrics' out‑of‑the‑box dashboards have **no energy / kWh
  / electricity‑cost widget** (verified across all 19 widgets). Per‑machine **energy‑to‑cost** is a
  differentiator (e.g. Guidewheel positions around it). **We already meter electricity per machine
  and convert to cost** — so this is a strength to keep and feature, not build from scratch.

### 3.2 Product costing / ERP (SAP, Oracle, Dynamics 365, Infor, MRPeasy)
- **Product cost = Direct Materials + Direct Labour + Manufacturing Overhead**; overhead includes
  **factory rent/depreciation, utilities, machine maintenance, tooling amortization** (OpenStax
  Managerial Accounting 4.2; CFI; NetSuite; AccountingTools). *Our module has material, labour,
  and electricity but is missing depreciation, mould amortization, maintenance, and general
  overhead.*
- **Cost roll‑up**: multi‑level BOM explosion rolls lower‑level costs up into the finished good
  (SAP CK11N/CK13N/CK40N; Oracle Cost Rollup; Dynamics 365 BC "standard cost"). *Analogue for us:
  preform (bought) + conversion → finished bottle cost.*
- **Standard vs Actual costing**: **standard** costing suits **high‑volume, homogeneous, repetitive
  production** (our case exactly) with small distortions; **actual/job** costing suits custom,
  low‑volume, volatile work (Meaden Moore; MRPeasy; Fabrico; NetSuite). → We should run a
  **standard cost** per bottle and compare actuals to it.
- **Variance analysis** is the core control loop of standard costing: **material price variance**
  (actual vs standard purchase price), **material usage/yield variance** (actual consumption vs
  standard), **labour rate & efficiency**, **overhead spending & volume/absorption**, plus **scrap**
  and **production‑order** variances (AccountingCoach; OpenStax Ch.8; ACCA; sysgenpro ERP list).
- **Activity‑Based Costing**: allocate overhead by **activity cost drivers** (machine‑hours,
  energy) rather than one blanket rate (Kaplan/Cooper; NetSuite; AccountingCoach) — relevant if you
  want to attribute shared plant overhead fairly across the two machines/companies.

### 3.3 Analytics / BI UX for operators & managers
- **Operator UX that "feels good"**: minimal fields, big touch targets, one screen, **reason‑coded
  scrap**, live validation, immediate feedback. MES vendors optimize the frontline screen precisely
  because adoption depends on speed of entry.
- **Manager UX**: KPI cards, **trend lines**, **Pareto**, **multi‑plant/multi‑machine comparison**,
  goal tracking, and drill‑down. MachineMetrics ships Multi‑Plant Comparison and KPI Matrix widgets
  — the two‑company (oil vs beverage) comparison is a natural fit for us.

### 3.4 Make‑vs‑buy decision frameworks (Umbrex TCO, BCG, ISM, CFI)
- **Total Cost of Ownership**, not sticker price, drives the decision. Standard TCO buckets (Umbrex
  make‑buy framework, corroborated by ISM and CFI):
  - **Direct**: materials, labour, overhead, supplier price
  - **Yields/learning**: scrap, rework, ramp curves
  - **Logistics & duties**: freight, tariffs, brokerage, packaging, insurance
  - **Working capital**: inventory days, payment terms, safety stock (carrying cost **18–25 %/yr**)
  - **Quality & service**: field returns, warranty, late penalties, expediting
  - **Management/transaction**: supplier management, audits, engineering support
  - **Risk premium**: expected cost of disruption = probability × impact
- **For "make" specifically, add**: **CapEx & depreciation** (equipment, tooling, fixtures),
  **utilities, maintenance, and the learning curve** (Umbrex, verbatim; CFI; Wall Street Prep).
- **Scale & utilization**: *"under‑utilized in‑house assets inflate unit cost, while suppliers may
  offer economies of scale"* (Umbrex) — i.e. model **realistic volumes**; a specialist high‑volume
  bottle supplier spreads fixed cost over far more units than two in‑house machines can.
- **Qualitative factors** routinely included: quality control, IP/know‑how, supply reliability &
  lead time, flexibility, and strategic dependence (BCG "Maximizing the Make‑or‑Buy Advantage").

---

## 4. Gap analysis — best practice vs. current module

| Capability (best practice) | Current module | Gap / action |
|---|---|---|
| Full product cost (material+labour+overhead+**depreciation+tooling+maintenance**) | Material, labour, electricity only | **Add** depreciation, mould amortization, maintenance, overhead, QA (R1) |
| Preform/resin cost booked on the run | Only used inside wastage calc | **Add** preform cost as the largest cost line (R1) |
| Buy‑side price / landed cost | None | **Add** finished‑bottle buy price + freight + carrying (R2) |
| Make‑vs‑buy comparison & breakeven | None | **Build** decision dashboard (R3) |
| Fixed vs variable split; cost‑vs‑capacity | Net cost per run only | **Add** capacity model & absorption (R4) |
| Standard cost + variance analysis | Actuals only | **Add** standard cost & variances (R5) |
| OEE / utilization / cycle‑time KPIs | Rejection %, units | **Add** utilization, availability, throughput KPIs (R6) |
| Downtime capture + Pareto | None | **Add** downtime log + Pareto (R6) |
| Reason‑coded scrap/rejection | Rejection pcs only | **Add** reject reason codes (R6) |
| Operator fast‑entry UX, live cost preview | Standard form | **Improve** entry UX + live preview (R6) |
| Trend / comparison / benchmark charts | Tables | **Add** charts, 2‑company comparison (R6) |
| Per‑machine energy‑to‑cost | ✅ Present | **Keep & feature** — ahead of MES peers (R7) |
| Sensitivity / scenario analysis | None | **Add** resin/tariff/volume/reject sliders (R3) |

---

## 5. The corrected fully‑loaded cost model (the "Make" side)

**Per‑bottle make cost** should roll up like a standard cost (all ₹/bottle):

```
Make cost/bottle =
    Preform cost                         (preform ₹/kg × preform weight kg)      ← ~90% of total
  + Blowing conversion:
      Electricity                        (total_units × ₹/unit) ÷ good bottles
      Operator + labour                  (manpower cost) ÷ good bottles
      Utilities/compressed air           ÷ good bottles
  + Wastage                              (reject_pcs × preform wt × preform ₹/kg) ÷ good bottles
  − Scrap recovery                       (bottle + carton scrap sold) ÷ good bottles
  + Mould amortization                   (mould cost ÷ mould life in shots)            ← NEW
  + Machine depreciation                 (machine ₹ ÷ life‑bottles, or ₹/day ÷ day output) ← NEW
  + Maintenance                          (₹/period ÷ output)                            ← NEW
  + Factory overhead                     (allocated ₹ ÷ output, ABC by machine‑hours)   ← NEW
  + QA / rework                          (quality cost ÷ output)                        ← NEW
  + Packing                              (fixed ₹/bottle)
```

Notes grounded in the research:
- **Yield basis:** divide by **good bottles = production − rejects**, not gross, so rejection
  correctly inflates unit cost (standard costing yield principle).
- **Stretch‑blow yield:** material = `preform weight` per good bottle; **no parison/flash term**
  (process correction, §1). The 4‑part cost engineering structure (material + tooling amortization
  + machine time + secondary ops) is valid; only the extrusion "÷ utilization" material term is not.
- **Fixed‑cost absorption:** depreciation, mould, and overhead are **fixed**; at 50 % utilization
  the fixed ₹/bottle roughly **doubles** (NetSuite; ACCA; rework.com). This is why the model must
  carry a **capacity/volume** input, not just per‑run actuals.

**Worked example (40 g bottle, shop's own rates):**
resin/preform ≈ 0.040 kg × ₹159/kg = **₹6.36** + conversion+packing ≈ **₹0.68** = **≈ ₹7.04/bottle
variable make cost**, *before* depreciation/mould/overhead. Those fixed adders (say ₹0.3–0.8/bottle
at good utilization, more when idle) put a realistic **fully‑loaded make cost ≈ ₹7.4–7.9/bottle** —
a number that only becomes meaningful next to a **buy price**.

---

## 6. The Make‑vs‑Buy decision model

**Buy cost/bottle (landed):**
```
Buy cost/bottle = supplier price
               + inbound freight & handling
               + duties (if any)
               + inventory carrying cost (18–25%/yr × avg inventory value)
               + incoming QA / rejection allowance
               + risk premium (expected disruption cost = probability × impact)
```

**Decision rule (per size, per company):**
- `Make cost/bottle < Buy cost/bottle` → **make is cheaper** at current volume.
- Because make carries **fixed** cost, compute the **breakeven volume**:
  ```
  Breakeven bottles/period = Fixed make cost per period
                             ÷ (Buy price − Variable make cost per bottle)
  ```
  Above breakeven, making wins; below it, buying wins. Plot make vs buy as lines crossing at
  breakeven (the classic make‑vs‑buy chart).
- **Contribution / margin framing:** `Buy price − Variable make cost` is the per‑bottle saving that
  must cover fixed cost. If it's negative, making never pays regardless of volume.

**Sensitivity / scenario (sliders in the UI):** resin ₹/kg, electricity tariff ₹/unit, rejection %,
monthly volume, machine utilization, buy price. Show how the verdict flips — e.g. "if resin rises
₹10/kg, breakeven moves from X to Y bottles/month." (Umbrex "model realistic volumes"; standard
sensitivity practice.)

**Qualitative scorecard (shown beside the numbers, not hidden):** quality control & consistency,
supply reliability & lead time, capacity flexibility, IP/know‑how, and strategic dependence on a
single supplier (BCG make‑or‑buy framework). A cheap buy price with a fragile single supplier can
still lose.

**Presentation:** a one‑screen verdict — big **Make ₹ vs Buy ₹** per bottle, the **monthly savings
run‑rate** (₹/month and annualized), the **breakeven vs current volume**, a **sensitivity mini‑panel**,
and the qualitative scorecard. This is the artifact leadership will actually look at.

---

## 7. Costing‑method upgrades (control loop)

1. **Standard cost per bottle** per size/machine (the budgeted make cost) — appropriate because
   this is high‑volume homogeneous production (Meaden Moore; MRPeasy).
2. **Variance analysis** each run/month, decomposed the standard way:
   - **Material price variance** — preform ₹/kg actual vs standard (sourcing signal)
   - **Material usage/yield variance** — reject % / grams per bottle vs standard (process signal)
   - **Energy variance** — units/bottle actual vs standard (machine health / tariff signal)
   - **Labour rate & efficiency** — manpower cost & bottles/manhour vs standard
   - **Overhead volume/absorption** — under‑utilization penalty
3. **Alerts** when a variance breaches a threshold (e.g. reject % > 0.5 %, energy/bottle > +10 %).

This converts the module from a ledger into a cost‑control system — exactly what ERP costing modules
do (SAP/Oracle/Dynamics), and cheap to add on top of the standard/actual we already snapshot.

---

## 8. UX / product recommendations ("feel‑good")

**Operator run entry (shop floor, tablet‑friendly):**
- One screen, large fields, keyboard‑less where possible; **only ask for what can't be derived**
  (meter readings, boxes, counts, rejects+reason). Auto‑fill rates & preform spec.
- **Live cost preview** as they type — per‑bottle cost updates in real time, green/amber vs
  standard. Immediate feedback is the single biggest "feels good" lever (MES operator‑screen pattern).
- **Reason‑coded rejection** (mould fault, preform defect, startup, temperature, changeover) → feeds
  a Pareto (MachineMetrics scrap‑reason pattern).
- Validation: stop > start meter, reject ≤ production, warn on outliers vs history.

**Manager dashboard:**
- **KPI cards**: per‑bottle cost (vs standard), bottles produced, rejection %, **utilization %**,
  energy/bottle, **make‑vs‑buy delta**.
- **Charts**: per‑bottle cost trend; cost **waterfall** (preform → +conversion → +overhead → −scrap
  → net); **rejection Pareto**; **two‑company / two‑machine comparison**; cost‑vs‑utilization curve.
- **Make‑vs‑Buy panel** (from §6) front‑and‑centre with the monthly verdict and savings run‑rate.
- **Energy** view — per‑machine kWh→₹, the capability MES peers lack (feature it).

**Polish that drives adoption:** fast load, skeleton loaders, empty‑states that guide setup (e.g.
"add a buy price to unlock make‑vs‑buy"), inline explanations of each cost line, export to
Excel/PDF for the leadership meeting, and a monthly auto‑generated make‑vs‑buy summary.

---

## 9. Phased roadmap

**Phase 1 — Complete the cost picture (make it *correct*).** Add preform cost line, mould
amortization, machine depreciation, maintenance, overhead, QA to the run/cost model; divide by good
bottles; keep the SBM wastage model. *Outcome: a true fully‑loaded make cost.*

**Phase 2 — Add the Buy side & the decision.** Buy price + landed cost per size/company; make‑vs‑buy
dashboard with breakeven and monthly verdict + savings run‑rate. *Outcome: the module answers its
core question.*

**Phase 3 — Control loop.** Standard cost + variance analysis + threshold alerts. *Outcome: cost
control, not just reporting.*

**Phase 4 — Sensitivity & scenarios.** Resin/tariff/volume/reject sliders; scenario save/compare.
*Outcome: robust decisions under uncertainty.*

**Phase 5 — UX & analytics polish.** Live cost preview, KPI cards, Pareto/trend/comparison charts,
utilization/OEE, energy view, exports. *Outcome: adoption and "feel‑good".*

---

## 10. Open questions / data we need from the business

1. **Buy price** of a finished blown bottle per size (the single most important missing input), and
   whether a credible supplier quote exists.
2. **Resin/preform price feed** — is preform ₹/kg fixed monthly or volatile? (drives the biggest
   variance).
3. **Machine & mould economics** — machine capital cost & useful life; mould cost & life in shots;
   maintenance ₹/period.
4. **Overhead** — how much shared factory overhead to allocate to blowing, and on what driver
   (machine‑hours suggested).
5. **Carrying cost %** and inbound freight for bought bottles.
6. **Target volumes** per size (to place current output against breakeven).
7. Are we deciding **buy finished bottle** vs **buy preform + blow** (assumed here), or also
   considering **buy resin + injection‑mould preform in‑house** (a bigger CapEx question)?

---

## Implementation status (built)

All five phases are implemented in the `blowing` app + `production/blowing` frontend:

- **Phase 1 — fully-loaded make cost.** `BlowingRunCost` now carries preform, mould amortization
  (`PreformSpec.mould_cost/mould_life_bottles`), machine depreciation (`BlowingMachine.depreciation_per_day`),
  maintenance/overhead/QA (`BlowingRateConfig.*_per_day`), a fixed/variable split, and a
  `make_cost_per_bottle` over **good bottles**. Run detail shows the breakdown.
- **Phase 2 — buy side + decision.** `BottleBuyPrice` (landed cost) + `reports/make-vs-buy/`
  (per-size make vs buy, breakeven, savings, verdict). New **Make vs Buy** dashboard page.
- **Phase 3 — variance control loop.** `PreformSpec.std_*` targets + `reports/variances/`
  (Reports → Variances tab) flagging runs that breach standard cost / reject % / energy.
- **Phase 4 — sensitivity.** What-if sliders (resin, tariff, volume, buy price) recompute the
  verdict live on the Make vs Buy page.
- **Phase 5 — UX.** Verdict banner, KPI cards, and make-vs-buy comparison bars.

Cost formulas are unit-tested against Linear.xlsx row 1 in `blowing/tests.py`. **Still deferred:**
SAP stock postings (GI preform / GR bottles) and richer charts. The one business input required to
make the decision live is a **finished-bottle buy price** per size (Master Data → Buy Prices).

## Sources

**Make‑vs‑buy / TCO:** Umbrex "Make‑or‑Buy / TCO framework"; Boston Consulting Group, *Maximizing the
Make‑or‑Buy Advantage*; Institute for Supply Management (TCO); Corporate Finance Institute
(make‑or‑buy, product costs); Wall Street Prep (tooling depreciation & cost of capital);
Accountingverse; AuraVMS (landed cost, carrying 18–25 %/yr).
**Product costing / standard vs actual / variance / rollup:** OpenStax *Principles of Managerial
Accounting* (4.2 product costs; Ch.8 variances); AccountingCoach; AccountingTools; ACCA Global
(fixed‑overhead absorption); Lumen Learning; Meaden Moore, Fabrico, MRPeasy, NetSuite, VisualSouth
(standard vs actual applicability); SAP CK11N/CK13N/CK40N, Oracle Cost Rollup / JD Edwards,
Microsoft Dynamics 365 BC, Infor SyteLine (cost roll‑up); sysgenpro (ERP variance types).
**MES / dashboards / operator UX:** MachineMetrics product docs (dashboards guide — 19 widgets/KPIs;
"Core Four MES Features"; "Empower Your Operators" operator interface; ShopPulse); Guidewheel
(energy‑monitoring positioning).
**Fixed‑cost absorption / utilization:** NetSuite (machine‑utilization rate); ACCA Global; Wiss;
rework.com; eCampusOntario *Fundamentals of Operations Management*.
**Process (stretch vs extrusion blow molding):** Boothroyd Dewhurst DFMA blow‑molding cost page;
Plastics Technology; PatSnap/Eureka (extrusion utilization 75–85 %) — cited to establish that these
**extrusion** models do **not** apply to PET **stretch** blow molding.

*Methodology: findings gathered via a multi‑agent research pass (fan‑out web search → source fetch →
3‑vote adversarial verification). Product‑feature claims were verified against vendor primary docs;
methodology claims against accounting authorities. Claims that failed verification — notably the
extrusion "parison ÷ utilization" material model — were excluded from the recommendations and are
flagged as process‑inapplicable above.*
