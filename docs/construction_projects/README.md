# Construction Projects — Plan

> **Django app:** `construction_projects`
> **URL prefix:** `/api/v1/construction/`
> **Frontend module:** `FactoryFlow/src/modules/construction/`
> **Eleven tables. Three phases. No measurement book, no earned value.**

---

## 1. What this module does

The factory is always building something — a shed, a wall, a tank foundation, a
new floor. Each of those is a project, and five things need to be true about it:

1. **It exists** — with a name, a place, a start, and a date it is expected to
   finish.
2. **It has a budget**, and somebody approved that budget.
3. **Somebody writes down what happened each day** — what got done, how many
   people were there, and whether work stopped.
4. **Every rupee spent is recorded against the day it was spent**, with what it
   was spent on.
5. **When it needs more money or more time, that is asked for and approved** —
   not absorbed quietly.

That is the whole module. Everything below serves that loop.

### The loop, physically

```
   Site in-charge, end of the day        Whoever holds the cash
   ┌─────────────────────────────┐      ┌─────────────────────────────┐
   │ "Today we cast the slab.    │      │ "₹31,500 cement, ₹7,200     │
   │  14 workers."               │      │  mason wages, ₹3,300 sand"  │
   └─────────────────────────────┘      └─────────────────────────────┘
                 │                                    │
                 ▼                                    ▼
             DailyLog                    lines pile into the OPEN batch
                 │                                    │
                 │                          site sends it ─► ONE approval
                 │                                    │
                 └──────────────────┬─────────────────┘
                                    ▼
   ┌──────────────────────────────────────────────┐
   │ Project header, live:                        │
   │   Budget    ₹8,00,000                        │
   │   Spent     ₹6,42,000   (80%)                │
   │   Left      ₹1,58,000                        │
   │   Ends      31 Mar  ·  24 days left          │
   └──────────────────────────────────────────────┘
                      │
          budget running out, or date slipping
                      ▼
   ┌──────────────────────────────────────────────┐
   │ Revision: "+₹3,00,000, new end 31 May.       │
   │  Reason: foundation depth revised after       │
   │  soil test."          → approved by X         │
   └──────────────────────────────────────────────┘
```

### What this module deliberately does not do

No work breakdown structure. No bill of quantities. No measurement book. No
running-account bills, retention, TDS or liquidated damages. No critical path. No
earned value. No tendering or quotation comparison.

Those belong to a firm that *contracts* construction. This factory *has*
construction done, and wants to know what it is costing and whether it will
finish on time. If contractor billing is ever needed, it is a separate module
that reads this one — not a reason to make this one complicated now.

---

## 2. The eleven tables

All inherit `gate_core.models.base.BaseModel` (`created_at`, `updated_at`,
`created_by`, `updated_by`, `is_active`). All money is
`DecimalField(max_digits=14, decimal_places=2)` — never a float.

### 2.1 `Project`

```python
company             FK company.Company, PROTECT
code                CharField(20)              # PRJ-2026-001, generated
                                           # unique_together with company
name                CharField(200)             # "New packing shed, Block C"
description         TextField(blank)
location            CharField(200, blank)      # "Block C, north side"

# how big it is -- all optional, and a unit because "30 x 20 x 12" alone is
# ambiguous and a site working in feet against a CUM rate is off by 35x
length              Decimal(10,2, null)
breadth             Decimal(10,2, null)
height              Decimal(10,2, null)
dimension_unit      CharField(choices=DimensionUnit)   # FT | M

start_date          DateField
expected_end_date   DateField                  # the estimated ending
actual_end_date     DateField(null, blank)

estimated_cost      Decimal(14,2)              # the budget being asked for
manager             FK User, PROTECT           # who runs it
site_incharge       FK User, SET_NULL, null    # who fills the daily log

status              CharField(choices=ProjectStatus, default=DRAFT, db_index=True)
# DRAFT → PENDING_APPROVAL → APPROVED → IN_PROGRESS → COMPLETED
#                          ↘ REJECTED      ↕ ON_HOLD        ↘ CANCELLED

submitted_at        DateTimeField(null)
submitted_by        FK User, SET_NULL, null
approved_at         DateTimeField(null)
approved_by         FK User, SET_NULL, null
decision_note       CharField(255, blank)

# --- roll-ups: recomputed by the service, never accepted as API input -------
sanctioned_budget   Decimal(14,2, default=0)   # estimated_cost + approved revisions
spent_amount        Decimal(14,2, default=0)   # sum of expenses
progress_percent    Decimal(5,2,  default=0)   # latest from the daily log

Meta:
    ordering = ["-created_at"]
    indexes  = [("company", "status"), ("manager",), ("expected_end_date",)]
    permissions = [ ... see §4 ... ]
```

Approving the project sanctions both the money and the date. There is no separate
budget document — `estimated_cost` becomes `sanctioned_budget` on approval, and
`expected_end_date` is the date everything is measured against.

The three roll-ups exist so a list of 40 projects is one query. They are
refreshed by `services.recompute_totals(project)`, called from every place that
changes money or progress. Nothing ever *decides* anything from them — the
source is always `Expense` and `ProjectRevision`.

### 2.2 `EstimateLine` — the sheet the job was costed from

```python
project   FK Project, CASCADE, related_name="estimate_lines"
line_no   PositiveIntegerField        # Sr. No. on the sheet
material  CharField(200)              # "CEMENT", "BRICKS", "STEEL 8MM"
quantity  Decimal(14,3)
unit      CharField(20, blank)        # BAG, CFT, SFT, CUM, NOS, KG, RFT, MT
rate      Decimal(14,2)
amount    Decimal(16,2)               # quantity x rate, stored so SQL can sum
notes     CharField(255, blank)
```

The spreadsheet a site actually costs from — 700 bags of cement at 350, 111,000
bricks at 7, a total at the bottom. Optional: a small job carries a round number
and no detail.

> **When any line exists, the lines ARE the estimate.** `estimated_cost` is
> recomputed from their sum and a figure typed beside it is discarded, because
> two numbers that are meant to agree eventually will not — and then nobody can
> say which one was sanctioned. Clearing the sheet hands the figure back to
> being typed.

Saved wholesale through `PUT projects/<id>/estimate/`, because it is a table:
somebody pastes twenty rows out of a spreadsheet, renumbers two and deletes one,
then saves once. Editable only while the project is a draft, for the same reason
`estimated_cost` is.

`unit` is free text rather than a master. Construction quotes in BAG, CFT, SFT,
CUM, NOS, KG, RFT and MT depending on the material, and `gate_core.UnitChoice`
is about vehicles arriving, not concrete.

### 2.3 `ProjectAttachment` — the papers behind the project

```python
project  FK Project, CASCADE, related_name="attachments"
file     FileField(upload_to="construction/projects/")
title    CharField(200, blank)   # optional: a filename often says it already
```

The quotation it was costed from, a drawing, the sanction letter, the completion
certificate. Distinct from `DailyLogPhoto`, which belongs to one day — these are
the project's paperwork, not the record of a shift.

**Uploadable at any status**, including a draft and a finished project, and
deliberately *not* gated the way the daily log is: the quotation exists before
approval (it is what the project was costed from), and the completion
certificate arrives after the work. Gating this on "project is live" would lock
out both ends of its life.

Because a project's papers usually exist before the project record does, the New
Project form holds the chosen files in memory and uploads them once the project
has an id. A failed upload there reports separately — the project *was* created,
and saying otherwise would send somebody to create it twice.

### 2.4 `ProjectRevision` — more money, more time, or both

```python
project            FK Project, CASCADE, related_name="revisions"
revision_no        PositiveIntegerField        # 1, 2, 3 ... within the project

additional_amount  Decimal(14,2, default=0)    # extra budget asked for
new_end_date       DateField(null, blank)      # the new expected ending
reason             TextField                   # required, always

# what it looked like when the request was made, so the approver sees the before
budget_before      Decimal(14,2)
end_date_before    DateField

status             CharField(choices=RevisionStatus, default=PENDING)
                   # PENDING, APPROVED, REJECTED, WITHDRAWN
requested_by       FK User, SET_NULL, null
requested_at       DateTimeField(auto_now_add=True)
decided_by         FK User, SET_NULL, null
decided_at         DateTimeField(null)
decision_note      CharField(255, blank)

Meta:
    ordering = ["project_id", "revision_no"]
    unique_together = ("project", "revision_no")
```

**One model for both kinds of extension**, because that is how it actually
happens — "we need two more months and three lakh more" is one conversation and
one approval, not two. Either field may be empty; at least one must be filled.

`budget_before` and `end_date_before` are snapshots taken at request time. They
cost two columns and they turn the approval screen from "₹3,00,000 — approve?"
into "₹8,00,000 → ₹11,00,000, 31 Mar → 31 May", which is the difference between
an approval and a rubber stamp.

On approval the service updates `project.expected_end_date` and recomputes
`sanctioned_budget`. Nothing is edited in place; the revision row is the record
of why the numbers changed.

### 2.5 `DailyLog` — what happened today

```python
project          FK Project, CASCADE, related_name="daily_logs"
log_date         DateField

work_done        TextField                     # "Cast the slab, grid A1-A6"
workers_count    PositiveIntegerField(default=0)
progress_percent Decimal(5,2, null, blank)     # cumulative, optional

work_stopped     BooleanField(default=False)
stopped_reason   CharField(choices=StopReason, blank)
                 # RAIN, NO_MATERIAL, NO_LABOUR, NO_POWER, HOLIDAY,
                 # APPROVAL_PENDING, SAFETY, OTHER
notes            TextField(blank)

Meta:
    unique_together = ("project", "log_date")
    ordering = ["-log_date"]
```

One row per project per day. No approval, no verification — it is a diary, and a
diary that needs signing off stops being written.

`stopped_reason` is a choice rather than free text for one reason: it makes "we
lost 11 days to rain and 4 waiting for material" a query instead of a reading
exercise, and that sentence is the whole justification for a timeline extension.

### 2.6 `DailyLogStopReason`

```python
daily_log  FK DailyLog, CASCADE, related_name="stop_reasons"
reason     CharField(choices=StopReason)
           # RAIN, NO_MATERIAL, NO_LABOUR, NO_POWER, HOLIDAY,
           # APPROVAL_PENDING, SAFETY, OTHER

Meta:
    unique_together = ("daily_log", "reason")
    default_permissions = ()
```

**A child table rather than a single choice on the log**, because a real day is
often both: it rained *and* the material had not arrived. Normalised rather than
a comma-separated string or a Postgres `ArrayField` — the array would be simpler
but no model in this repo uses Postgres-only fields, and the child table keeps
the roll-up a single `GROUP BY`.

Plain `models.Model`: a tag on a day, with nothing of its own to audit.

> The per-reason counts in `spend-summary` can sum to **more** than
> `days_lost_total`. A day that both rained and ran out of material is one lost
> day appearing under two reasons. That is the honest answer to "why did we lose
> time", not a double count to be corrected.

### 2.7 `DailyLogPhoto`

```python
daily_log  FK DailyLog, CASCADE, related_name="photos"
photo      FileField(upload_to="construction/daily-logs/")
caption    CharField(200, blank)
```

A photo a day is the cheapest progress record there is, and it settles arguments
six months later that nothing else can.

### 2.8 `ExpenseBatch` — a project's spend, settled in one decision

```python
project        FK Project, CASCADE, related_name="expense_batches"
batch_no       PositiveIntegerField
status         CharField(choices=ExpenseBatchStatus)
               # OPEN -> SUBMITTED -> APPROVED | RETURNED
submitted_at / submitted_by
decided_at / decided_by / decision_note

constraints = [UniqueConstraint(fields=["project"],
                condition=Q(status__in=["OPEN","RETURNED"], is_active=True),
                name="uniq_open_expense_batch_per_project")]
```

Payments are **not approved one at a time**. Lines pile into the project's open
batch as the site records them; the site sends it; one decision settles every
line in it. That matches how a site is actually reviewed — *"this week's spend is
fine"* — rather than making somebody click through a hundred cement bills.

The partial unique constraint keeps exactly one batch a project may add to. Two
would split a day's spend across claims by accident.

| State | Meaning |
|---|---|
| `OPEN` | Collecting. The site adds, edits and deletes freely |
| `SUBMITTED` | With the approver. Locked to the site |
| `APPROVED` | Settled. Every line in it is approved, permanently |
| `RETURNED` | Sent back with a reason. Editable again, and re-sendable |

> **A return is not a disallowance.** Nothing is ever un-spent — the cash left
> the box. The batch simply goes back to be corrected and sent again. A reason
> is required (§3.10); an approval needs none.

### 2.9 `Expense` — what the money went on

```python
project      FK Project, PROTECT, related_name="expenses"
batch        FK ExpenseBatch, PROTECT, related_name="expenses"
spend_date   DateField
category     CharField(choices=ExpenseCategory)
description  CharField(300)          # "90 bags cement"
amount       Decimal(14,2)
paid_to      CharField(200, blank)   # typed in, not a vendor master
payment_mode CharField(choices=PaymentMode)   # CREDIT = incurred, not yet paid
reference_no CharField(60, blank)
bill         FileField(null, blank)
```

No decision of its own — it belongs to a batch, and the batch is what somebody
approves. A line is editable only while its batch is.

**Recorded on its own screen, not in the daily log.** Spend used to be part of
the day form; burying it there made a page half the site could not submit, and
it tied a payment's fate to a diary entry it has nothing to do with.

**Every recorded line counts against the budget**, approved or not. The cash
left the box, and a project sitting on an unreviewed batch must not read as under
budget — which is the failure this module exists to prevent.

`paid_to` is typed in, not picked from a master. Construction buys from whoever
has cement that morning, and forcing a vendor master on it means the field gets
filled with "OTHER".

`CREDIT` matters: material taken on credit is spent money even though it has not
left the bank. Counting it only on payment makes a project look under budget
right up until the day it doesn't.

### 2.10 `ProjectSequence`

```python
company     FK Company, CASCADE
year        PositiveIntegerField
last_number PositiveIntegerField(default=0)

Meta:
    unique_together = ("company", "year")
```

Fifteen lines that stop two people creating `PRJ-2026-007` at the same time:

```python
with transaction.atomic():
    seq, _ = ProjectSequence.objects.select_for_update().get_or_create(
        company=company, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"PRJ-{year}-{seq.last_number:03d}"
```

> Do not copy `construction_gatein.ConstructionGateEntry._generate_work_order_number()`.
> It reads the highest existing number and adds one, which hands two concurrent
> creates the same number. Fine on a gate with one operator; wrong here.
> `gate_core.SalesDispatchGatepassSequence.next_gatepass_no()` is the pattern to
> copy.

---

## 3. The rules that matter

Only fourteen. Each is one service function and one test.

### 3.1 Status transitions are service methods, not PATCHes

`status` is read-only on the serializer. `submit()`, `approve()`, `reject()`,
`hold()`, `resume()`, `complete()`, `cancel()` are the only ways it moves.

- `APPROVED` → `IN_PROGRESS` happens automatically on the first daily log, because
  that is what starting actually is.
- `COMPLETED` requires `actual_end_date`. It does not require the budget to be
  spent or the project to be on time — real projects finish over and under.
- `CANCELLED` is refused once any expense exists. Money was spent; the project
  happened. It completes or it stays on hold, but it did not un-happen.

### 3.2 Editing stops at approval

`estimated_cost`, `start_date` and `expected_end_date` are editable while `DRAFT`
or `REJECTED`. Once approved, they change only through a `ProjectRevision`. The
serializer drops the fields rather than erroring, so the UI simply shows them
read-only.

### 3.3 Overspending is recorded, then flagged

An expense that takes `spent_amount` past `sanctioned_budget` is **saved**, not
refused. The money is already gone; refusing to record it only makes the books
wrong while the shed still gets built.

What happens instead: the API returns the expense with a warning block, and every
read of the project carries the overrun.

```jsonc
{ "expense": { ... },
  "warning": { "code": "budget_exceeded",
               "sanctioned": "800000.00", "spent": "842000.00",
               "over_by": "42000.00",
               "message": "This project is now ₹42,000 over its sanctioned budget. Raise a revision." } }
```

The UI turns that into a banner with a "Request more budget" button that opens
the revision form pre-filled with the shortfall. That is the loop closing itself.

### 3.4 A revision must ask for something

At least one of `additional_amount > 0` or `new_end_date` must be given, and
`reason` is never blank. A revision asking for nothing is a status update, and
those belong in the daily log.

`new_end_date` must be later than the current `expected_end_date` — this module
extends timelines. Pulling a date in is an edit to a plan nobody has started
missing yet, and it needs its own conversation, not this form.

### 3.5 One log per project per day

Enforced by `unique_together`. Filing a second log for the same date opens the
existing one for editing instead of erroring — the site in-charge who remembers
something at 9pm should not have to hunt for yesterday's row.

Back-dating is allowed up to 7 days (`CONSTRUCTION_BACKDATE_DAYS`, a setting).
Past that it needs `can_edit_project`. Sites get written up late; sites written
up three weeks late are being reconstructed from memory.

### 3.6 Progress only goes forward

`progress_percent` on a new daily log may not be lower than the last recorded
value, unless the writer holds `can_edit_project`. Work does not un-happen, and
a typo that drops a project from 60% to 6% otherwise sits there until somebody
notices the chart.

### 3.7 Money leaves the API as a string, never a float

Every amount, rate and percentage is a `Decimal` in Python and a string in JSON.
Model serializers get this right for free -- DRF's `DecimalField` stringifies --
but a composed read that builds a plain dict does not: DRF's JSON encoder turns
a raw `Decimal` into a float and silently loses paise. `services._money()`
quantises and stringifies, and every hand-built read payload goes through it.

### 3.8 Uploaded files come back as absolute urls

`photo` and `bill` are `SerializerMethodField`s that call
`request.build_absolute_uri()`, and **every view passes
`context={"request": request}`** when it builds a serializer.

A bare `FileField` serialises to `/media/construction/...`. The browser then
resolves that against whatever origin served the page — in development the Vite
dev server on `:5173`, not Django on `:8000` — and the image 404s. It shipped
that way the first time and the photo rendered as a broken icon.

This is not optional politeness: DRF only fills `self.context` when it is given,
and these views build their serializers by hand rather than through a generic
view, so nothing supplies it automatically. `tests/test_media_urls.py` asserts
the url is absolute on the write path *and* on all three read paths, because
each builds its own serializer and any one of them can forget.

Every module in this repo does the same — `cash_book`, `issues`,
`employee_hierarchy`, `ar_invoice`, `quality_control` — and there is no
media-url helper on the frontend, which renders `photo.photo` straight into a
`src`. The backend is where this is solved.

> **One app-wide caveat, not this module's to fix.** `settings.py` does not set
> `SECURE_PROXY_SSL_HEADER`. Behind a TLS-terminating proxy `build_absolute_uri`
> emits `http://`, which a browser blocks as mixed content. That would affect
> every module listed above equally; if production media ever comes back over
> `http`, it is one line in `config/settings.py`, not a construction change.

### 3.9 The estimate's breakdown is the estimate

With `EstimateLine` rows present, `estimated_cost` is their sum and a typed
figure is discarded (`update_project` drops the field). With none, it is typed.
Never both — see §2.2.

### 3.10 No more budget until the last lot is approved

`request_revision` refuses while any spend is unapproved
(`expenses_awaiting_approval`), and `project_summary` carries
`can_request_revision` so the UI does not offer the button.

Sanctioning fresh budget on top of spend nobody has checked is how a project
quietly doubles: the figure being revised is not yet known to be right. Approving
the batch is the gate, and it is the same gate whether the batch is still
collecting or already sitting with the approver.

### 3.11 A rejection carries a reason; an approval need not

`reject_project` and `reject_revision` refuse a blank note with
`rejection_needs_a_reason`. Approving is allowed silently.

Refusing somebody's budget without saying why leaves them with nothing to act
on — they cannot tell whether to re-cost it, get another quote, or drop it. The
rule is in the service, not only the dialog, because the dialog is the
convenience and the service is the floor.

### 3.12 Never offer a form that will be refused

A screen that collects a day's work and only objects on Save has thrown that
work away. The day page therefore checks the project's status **before
rendering the form** and shows a plain explanation instead, and every route into
it — the header button, the empty-state button, and each log's date link — is
gated on the same condition.

The backend already refused the write (`project_not_approved`); the bug was
offering the form at all. Server-side validation is the floor, not the
interface: if an action cannot succeed, do not present it.

### 3.13 Everything is company-scoped

Every queryset filters on `request.company.company`, resolved by
`company.permissions.HasCompanyContext`. A caller without
`can_view_all_projects` sees only projects where they are the `manager` or the
`site_incharge` — applied in `get_queryset()`, never in a serializer.

---

## 4. Permissions

Nine, and **exactly** nine. Declared on `Project.Meta.permissions`, enforced by
DRF permission classes in `permissions.py` copying `maintenance/permissions.py`'s
`DjangoPermission` shape.

Every one of the six models sets `default_permissions = ()`. Without it Django
adds `add`/`change`/`delete`/`view` for each — 24 rows this module never checks,
and a `view_project` sitting next to `can_view_project` in the group editor is a
footgun: granting the wrong one looks right and does nothing. The cost is that
Django admin for these models is superuser-only, which is what it is used for
here. `tests/test_permissions.py` fails if a seventh model reintroduces them.

| Codename | Grants |
|---|---|
| `can_view_project` | Projects the caller runs or is site in-charge of |
| `can_view_all_projects` | Every project in the company |
| `can_create_project` | Raise a project |
| `can_edit_project` | Edit a draft, back-date a log, correct progress |
| `can_approve_project` | Approve or reject a project **and** its revisions |
| `can_log_daily_work` | Write the daily log |
| `can_record_expense` | Record spend |
| `can_approve_expense` | Check the day's payments. A different job from sanctioning a budget, and usually a different person — the PM, not the director |
| `can_close_project` | Complete, hold, resume, cancel |

### Three groups

Created by `management/commands/setup_construction_groups.py`, idempotent and
re-runnable after any permission change.

> **Deliberately a command and not a `0002_create_..._group.py` migration**, which
> is what the rest of this repo does. That pattern does not work: permissions are
> created by a `post_migrate` signal that fires *after* every migration in the
> run, so a `RunPython` that filters `Permission.objects.filter(app_label=...)`
> finds nothing and silently creates an **empty** group. Verified here — the
> first build of this module shipped exactly that, a `construction_projects`
> group with zero permissions. Group setup belongs after `migrate`, not inside
> it.

| Group | Holds |
|---|---|
| `construction_site` | `can_view_project`, `can_log_daily_work`, `can_record_expense` |
| `construction_manager` | the above + `can_create_project`, `can_edit_project`, `can_close_project`, `can_view_all_projects`, `can_approve_expense` |
| `construction_approver` | `can_view_all_projects`, `can_view_project`, `can_approve_project` |

The approver group is deliberately separate and holds nothing else: the person
who sanctions the money does not also record the spend.

---

## 5. API

Base `/api/v1/construction/`. Every endpoint: `IsAuthenticated` +
`HasCompanyContext` + the named permission.

### Projects

| Method | Path | Permission |
|---|---|---|
| `GET POST` | `projects/` | `can_view_project` / `can_create_project` |
| `GET PATCH` | `projects/<id>/` | `can_view_project` / `can_edit_project` |
| `GET` | `projects/<id>/summary/` | `can_view_project` |
| `POST` | `projects/<id>/submit/` | `can_edit_project` |
| `POST` | `projects/<id>/approve/` | `can_approve_project` |
| `POST` | `projects/<id>/reject/` | `can_approve_project` |
| `POST` | `projects/<id>/hold/` · `resume/` | `can_close_project` |
| `POST` | `projects/<id>/complete/` · `cancel/` | `can_close_project` |

List filters: `status`, `manager`, `search` (code + name), `over_budget=true`,
`overdue=true`, `mine=true`.

`status` takes a **comma-separated list**, because the register's tabs are
groups rather than single statuses — *Live* is `APPROVED,IN_PROGRESS,ON_HOLD`.
A value that is not a status matches nothing rather than being dropped: a
filter that quietly returns the whole register reads as though it worked, which
is how this one being missing altogether went unnoticed.

`projects/<id>/summary/` is the header every screen shows:

```jsonc
{ "code": "PRJ-2026-004", "name": "New packing shed, Block C",
  "status": "IN_PROGRESS", "location": "Block C, north side",

  "sanctioned_budget": "1100000.00",   // 8L original + 3L revision
  "spent_amount":      "642000.00",
  "remaining":         "458000.00",
  "percent_used":      58.36,
  "is_over_budget":    false,

  "start_date": "2026-01-06", "expected_end_date": "2026-05-31",
  "days_elapsed": 78, "days_left": 42, "is_overdue": false,

  "progress_percent": "55.00",
  "last_log_date": "2026-03-24",
  "days_since_last_log": 1,

  "revisions": { "count": 1, "pending": 0 },
  "spent_today": "42000.00" }
```

`days_since_last_log` is there on purpose: a project nobody has written up for
five days is the first sign something has stalled, and it is visible without
opening anything.

### Revisions — more budget, more time

| Method | Path | Permission |
|---|---|---|
| `GET POST` | `projects/<id>/revisions/` | `can_view_project` / `can_create_project` |
| `GET` | `revisions/<rid>/` | `can_view_project` |
| `POST` | `revisions/<rid>/approve/` · `reject/` | `can_approve_project` |
| `POST` | `revisions/<rid>/withdraw/` | requester |

### The daily loop

| Method | Path | Permission |
|---|---|---|
| `GET POST` | `projects/<id>/daily-logs/` | `can_view_project` / `can_log_daily_work` |
| `GET PATCH` | `daily-logs/<lid>/` | `can_view_project` / `can_log_daily_work` |
| `POST DELETE` | `daily-logs/<lid>/photos/` | `can_log_daily_work` |
| `GET POST` | `projects/<id>/expenses/` | `can_view_project` / `can_record_expense` |
| `GET PATCH DELETE` | `expenses/<eid>/` | `can_record_expense` |
| `GET` | `projects/<id>/day/?date=YYYY-MM-DD` | `can_view_project` |
| `GET` | `projects/<id>/spend-summary/` | `can_view_project` |
| `GET` | `approvals/` | `can_approve_project` |

**`POST projects/<id>/daily-logs/` accepts the expenses inline.** This is the one
piece of API design in the module that matters, because it matches what a person
actually does at the end of a day:

```jsonc
{ "log_date": "2026-03-24",
  "work_done": "Cast the slab, grid A1-A6. Curing started.",
  "workers_count": 14,
  "progress_percent": "55.00",
  "expenses": [
    { "category": "MATERIAL", "description": "90 bags cement",
      "amount": "31500.00", "paid_to": "Verma Traders",
      "payment_mode": "CREDIT", "reference_no": "B-8841" },
    { "category": "LABOUR", "description": "Mason wages, 6 men",
      "amount": "7200.00", "payment_mode": "CASH" },
    { "category": "TRANSPORT", "description": "2 loads sand",
      "amount": "3300.00", "payment_mode": "CASH" } ] }
```

One transaction. The log and its expenses land together or neither lands. Photos
upload separately afterwards, because multipart plus nested JSON in one request
is a fight not worth having.

**`GET projects/<id>/day/?date=`** is the screen the whole module is for:

```jsonc
{ "date": "2026-03-24",
  "log": { "work_done": "...", "workers_count": 14, "work_stopped": false,
           "progress_percent": "55.00", "photos": [ ... ] },
  "expenses": [ ... ],
  "spent_today": "42000.00",
  "spent_to_date": "642000.00",
  "budget_remaining": "458000.00" }
```

`spend-summary/` returns spend grouped by category and by month, plus days lost
by `stopped_reason`. Two charts and the extension justification, from one call.

`approvals/` is the approver's queue: projects `PENDING_APPROVAL` and revisions
`PENDING`, in one list, oldest first.

---

## 6. Build order

Three phases, each independently deployable and each about a week.

### Phase 1 — The project and its budget

`startapp construction_projects`; add to `INSTALLED_APPS` and `config/urls.py`.

Models: `Project`, `ProjectSequence`. One `0001_initial` — no group migration,
see section 4. `constants.py` with every enum for all three phases in one go.

Services: one `services.py` -- six tables do not need a five-file package.
`ProjectSequence.next_code` (write and test this first; every later phase
depends on it and a numbering race found in phase 3 is found in production),
then the project transitions and `recompute_totals`.

Endpoints: the Projects block plus `summary/`. Permissions, groups command,
admin registration.

**Tests** — `tests/test_projects.py`:
- Two threads creating a project get two different codes.
- Company scoping: company A cannot read company B's project by id.
- Row-level scoping: a non-manager without `can_view_all_projects` gets 404.
- Every status transition, allowed and refused.
- `estimated_cost` is read-only once approved.
- Cancelling a project that has expenses is refused *(add once Phase 2 lands)*.

**Done when** a manager can raise a project with a budget and an end date, an
approver sanctions it, and it appears in a filtered list.

### Phase 2 — The daily loop

Models: `DailyLog`, `DailyLogPhoto`, `Expense`.

Services: `save_daily_log` (log plus expenses in one transaction, the back-date
window, the progress-only-forward rule) and `record_expense` (record, the
overrun warning, refresh the roll-ups).

Endpoints: daily-logs, photos, expenses, `day/`, `spend-summary/`.

**Tests** — `tests/test_daily.py`, `tests/test_expenses.py`:
- Log + three expenses in one POST; a failure on expense three rolls back the log.
- A second log for the same date opens the first, not a 400.
- Back-dating beyond the window needs `can_edit_project`.
- Progress cannot go backwards without `can_edit_project`.
- An expense past the budget saves and returns the `budget_exceeded` warning.
- `spent_amount` matches the sum of expenses after every write.
- `day/` returns the log, its expenses and the right running totals.

**Done when** a site in-charge fills one form on a phone at the end of the day and
the project header moves.

### Phase 3 — Extensions

Models: `ProjectRevision`.

Services: `revisions.py` — request (snapshotting `budget_before` /
`end_date_before`), approve (updates `expected_end_date`, recomputes
`sanctioned_budget`), reject, withdraw.

Endpoints: the Revisions block and `approvals/`.

Notifications through `notifications.services.NotificationService` — a new
`NotificationType` for "approval waiting" and "project over budget". No second
notifier.

**Tests** — `tests/test_revisions.py`:
- A revision asking for neither money nor time is refused.
- `new_end_date` earlier than the current end date is refused.
- Approving raises `sanctioned_budget` by exactly `additional_amount` and moves
  `expected_end_date`.
- Rejecting changes nothing on the project.
- The snapshot fields hold the values from request time even after a second
  revision lands.

**Done when** a project that has run past its budget gets ₹3 lakh and two months
more, approved, with the reason on the record.

---

## 7. Frontend

Built at `FactoryFlow/src/modules/construction/`, shaped like
`src/modules/issues/` — helpers in `utils.ts`, components in
`components/ConstructionBits.tsx`, one file per API area. Permissions in
`src/config/permissions/construction.permissions.ts`, endpoints in
`src/config/constants/api.constants.ts` under `CONSTRUCTION`, registered in
`src/app/registry/index.ts` after Maintenance.

```
src/modules/construction/
  module.config.tsx        six routes + the sidebar group
  utils.ts                 money/date formatting, label + tint maps
  types/index.ts           written from section 5, not from what the API returned
  api/
    construction.api.ts    every call, one method each
    construction.queries.ts hierarchical keys + one blunt invalidator
  components/
    ConstructionBits.tsx   StatusBadge, BudgetBar, TimeBar, Stat
    RevisionDialog.tsx     ask for more money, more time, or both
  pages/
    ProjectsListPage.tsx
    ProjectFormPage.tsx
    ProjectDetailPage.tsx
    ProjectDayPage.tsx     the one filled on a phone
    ConstructionApprovalsPage.tsx
```

### Routes

| Route | Screen | Permission |
|---|---|---|
| `/construction/projects` | List — one project per row, budget bar and time bar | view |
| `/construction/projects/:id` | Detail — header + three tabs | view |
| `/construction/approvals` | Projects and revisions waiting | `APPROVE_PROJECT` |

**Neither the project form nor the day has a route of its own.** Both were
pages once. A project is raised from the register and edited from the project,
and in both cases what you were looking at is the context for what you are
typing, so `ProjectFormDialog` opens over it instead of replacing it with a
breadcrumb trail. It is the same component for both jobs — passed a
`projectId` it edits, without one it creates.

**A draft may be almost empty.** `name`, `start_date`, `expected_end_date`,
`estimated_cost` and `manager` are nullable columns (migration `0009`), because
a draft is a form somebody started rather than a project that exists. The
requirement moved to `services.REQUIRED_TO_SUBMIT`, and `submit_project`
refuses an incomplete project naming each missing field — *"Still needed: when
it starts, who runs it."* `_validate_dates` returns early when either date is
missing, so the order check runs again at submit, when both are finally there.

Three model properties had to learn the same lesson: `days_elapsed`,
`days_left` and `is_overdue` answer `None`/`False` for a project with no dates
rather than raising. "How late is it?" has no answer for a project whose end
date has not been decided.

The form sends `null`, not `""`, for an untouched date or figure — DRF's
`DateField` and `DecimalField` reject the empty string even where they allow
null — and an **entirely untouched form saves nothing at all**: no request, no
row, because a project whose every column is empty is one nobody could come
back to.

**Two ways out of the form.** *Save as draft* writes the project and leaves
you where you were; *Create & send for approval* writes it and submits it in
the same breath. They are two calls, and the second can fail on its own — so a
failed submit says the project is saved as a draft rather than "could not
save", which would send somebody off to retype a project that already exists.
The send button appears only for somebody who holds `can_edit_project`, because
that — not `can_create_project` — is what `ProjectSubmitAPI` gates on; a
create-only user gets the draft button alone rather than one that 403s once
the project exists.

**A draft row on the register reopens this form** rather than going to the
project page: a draft is an unfinished form, so it resumes as one. Everything
else has a project behind it — days, spend, papers — and goes to the page that
holds them. Saving a new draft also moves the register off *Live*, which by
definition excludes drafts, so the row that was just written is actually
visible.

**The two people pickers search.** The staff list is the whole company, and a
hundred names in a dropdown is not a choice, it is a haystack — so both use the
shared `SearchableSelect` and list nothing until **two letters** are typed. That
threshold is a new `minSearchLength` prop on the shared component rather than a
`filterFn` that returns false for everything: one character is an unfinished
question, not a failed search, so it answers *"Type two letters of their name"*
rather than *"Nobody by that name"*. The prop defaults to 0, so every existing
caller behaves exactly as before.

**The map is uploaded as a map, and shown on its own.** `ProjectAttachment`
carries a `kind` — `MAP` or `DOCUMENT`, migration `0010` — and the Files tab
lists site maps above **Everything else**. The map answers *"where on the
campus, and what shape"*, which is the question people open that tab to ask,
and it should not have to be picked out of a list of sanction letters. It is a
*kind* rather than a `map` field on the project because a site plan and a floor
layout are both maps and nothing should stop the second one being uploaded.

> **Maps-first is annotated, not ordered.** `Meta.ordering = ["kind", ...]`
> looks right and is wrong: it sorts the stored string, and `"DOCUMENT"` sorts
> above `"MAP"`, so the paperwork came out on top. The model keeps its
> newest-first ordering and `ProjectAttachmentAPI.get` annotates the
> precedence. A test asserts the order, which is how this was caught.

**Attachments ask what a file is in both modes.** The *"What is it?"* box used
to render only on the Files tab; on the New Project form a file was staged as a
bare `File` and uploaded with no title. A drawing that reaches the approver
called `Screenshot From 2026-09-23 11-24-59.png` is no use to them, and the
moment somebody knows what a file is, is the moment they attach it. A staged
item is now `{ file, title }`, and it follows the same rule as the live panel:
one title describes one file, a batch of several keeps its own names.

**Explain** sits on the right of the budget field, opening the estimate sheet
on the figure it explains. On a project that does not exist yet the sheet
stages its lines instead of saving them — the same arrangement `AttachmentsPanel`
uses, because the breakdown is usually written before anybody presses Create —
and they are PUT once the project has an id. The typed budget follows the
breakdown's total, since the server recomputes `estimated_cost` from the lines
and two figures that are meant to agree eventually will not.

The form asks six things and explains none of them. It used to carry three
helper lines — that the budget is sanctioned on approval, that it can later be
broken down material by material, that the site in-charge fills the daily log —
and all three described something that happens somewhere else, on the one
screen where the reader can do nothing about it. The estimate sheet button went
with them: the detail page's menu already opens it, and one entry point is
enough.

The day has no route of its own. It was a page once and is now
`DayEntryDialog`, opened from a row of the daily table, because a day is read
against the rest of the project rather than instead of it. Opening it is gated
on *view* alone, so anybody who can see the project can read a past day; writing
is gated on `LOG_DAILY_WORK` **and** on the project's status, which is why the
dialog takes a `canWrite` prop — see below.

### The list

One project per row, not a grid of cards. The same facts either way, but a row
puts every project's money under every other project's money and every timeline
under every other timeline, so the page reads down a column instead of being
hunted around a grid. There is no header row: the bars label themselves
(`₹17,000 of ₹20,00,000`, `22 Sept → 01 Oct`), and a heading saying *Budget* over
a bar that already says so is the same sentence twice. The row collapses to a
stack below `lg`, so a phone still gets one project at a time.

### The detail page

A header plus four tabs — *Daily*, *Expenses*, *Revisions*, *Files*. The header
shows the size (`600 sq ft · 7,200 cu ft`) and an **Estimate in detail** link
opening the costing sheet. The header is a
budget bar and a time bar side by side, because over budget and over time are
the two things worth seeing at once. When spend passes the budget the bar turns
red and a **Request more budget** button appears beside it, opening the revision
form pre-filled with the shortfall; when the end date passes, the time bar does
the same with **Extend timeline**. That is the loop closing itself rather than a
dashboard that only scolds.

### The day screen

Built for a phone held in one hand at a dusty site:

- Work done — one big text area, and the only required field.
- People on site — a typeable number with `−` and `+` beside it. Typeable
  because a crew of 90 is one entry, not ninety taps; the buttons stay for
  nudging by one. Pre-filled from the last day written up, because it is usually
  the same crew and re-keying it daily is how daily logs stop being filled.
- Work stopped — a toggle revealing reason chips that **multi-select**, because
  a day can rain and run out of material at once.
- No spend. It moved to its own screen — see below — so the day form is short
  enough that a site in-charge actually files it.
- Photos — a camera button, each image shrunk to a 1600px edge JPEG in the
  browser before upload, because a phone camera makes 4MB files and a site
  uploads twenty. They go up after the log is saved: multipart plus nested JSON
  in one request is a fight not worth having.
- One Save, pinned to the bottom of the viewport on mobile.

### The daily log, as a sheet

`DailyLogTable` is the day list, and it is a table rather than a stack of cards
because of what gets asked of it: how many days did the rain cost us, which days
had forty men on site, what did last week come to. Those are all funnel-and-sort
questions, and the funnels are the same ones on the dispatch sheet and the
Expenses tab, so nobody has to learn a second way to filter.

A row holds the facts of a day — date, a truncated line of what got done, heads
on site, progress, stopped or worked, why, and the day's spend. The prose and the
photos do not fit a cell, so **clicking anywhere on the row opens the day** in
`DayEntryDialog`. There is no expand-in-place: the dialog already shows the whole
day and is where it is edited, and two ways to read the same entry is one too
many.

**Every row is clickable, not only the ones you may write.** The dialog opens as
a plain read view for somebody without `can_log_daily_work`, so the row click is
the only way to read a day in full and gating it would hide the diary from the
people most likely to be reading it. To make that safe, `DayEntryDialog` takes a
`canWrite` prop and ANDs it with the permission: the *status* gate lives on the
page — a `DRAFT` or `COMPLETED` project refuses the save whoever is asking — and
without it a foreman on a closed project would be offered a Save that comes back
refused. Its body is a `<fieldset disabled>`, so a read view cannot be typed
into; the date picker sits outside it, in the header, so days can still be
flicked through.

The row carries the click for the mouse and the date cell holds a real `<button>`
for the keyboard and the screen reader, which cannot reach a `<tr>`. Both call
the same setter with the same date, so the click bubbling out of the button is a
no-op rather than a second open — there is no `stopPropagation` anywhere in this
table, and that is the point.

> **One honest limit.** A day can be stopped for several reasons at once, and the
> shared kit filters on a cell's whole text, so the **Why** funnel offers
> `Rain, Safety` as a single entry rather than Rain and Safety separately. The
> plain **Stopped / Worked** column is the clean filter, and `spend_summary`
> counts days lost *per reason* properly. Splitting the funnel would mean
> teaching `useLocalColumns` about multi-valued cells — worth doing if this
> becomes the filter people reach for, and not before.

### The estimate sheet

`EstimateSheetDialog` is shaped like the spreadsheet it replaces — Sr. No.,
material, qty, unit, rate, amount, total — because that is what people have in
front of them. Rows add, renumber and delete freely and the whole thing saves
once. Pasting a tab-separated block into the material cell fills a whole block
of rows, which is exactly what the clipboard holds after selecting cells in
Excel or Sheets. Read-only once the project is approved.

### The Expenses tab

**Record a payment** and **Send for approval** sit in the tab row, to the right
of the tabs: an action on a whole tab belongs beside the tab, not stacked above
the table it acts on. Selection and the two dialogs they drive are therefore
held on the project page and passed into `ExpensesPanel`.

**Send for approval carries the selected total** — *Send for approval ·
₹21,642* — and is **faded rather than disabled** when nothing is ticked.
Clicking it then says *"Select an expense to send it for approval."* A disabled
button swallows the click and teaches nothing; the whole point of the faded
state is to be pressed by somebody who has not realised they need to tick a row.

The status card that used to sit above the table is gone. It announced *"Not
sent for approval yet"* over a table whose own **Approval** column already says
so on every row, and repeated the selected total that the button now carries.
Two things it said that nothing else does are kept as plain lines: the
approver's reason for sending payments back — losing that would leave the site
unable to find out what it was asked to fix — and the fact that a batch under
review cannot be touched.

The panel also carries the sentence that explains the revision gate: *no more
budget can be asked for until this is approved*.

### Approving and rejecting

Both open a `DecisionDialog` rather than firing on click. It puts the figures
being decided in front of the decider (`₹3,50,00,000 → ₹11,00,00,000`) and takes
a note — optional on an approval, **required on a rejection**, matching the
service. One component serves projects, revisions **and** expense batches, on the detail
page and in the approvals queue. A batch card shows the claim and its lines with
a single **Approve all**, because the decision is "this site's week is fine"
rather than a hundred separate ones.

### Data layer

Query keys are hierarchical — `['construction', 'project', <id>, …]` — and every
mutation calls one blunt `invalidateProject()`. It is one extra round trip and it
removes the whole class of bug where the header still shows yesterday's spend
after an expense was recorded.

`recharts` was already in `package.json`; the category breakdown is a plain
stacked bar and needed nothing new. No dependency was added on either side.

## 8. Repo conventions to hold to

From `CLAUDE.md`, and each of these has bitten this tree before:

**Migrations.** After every pull or push:
`.venv/bin/python manage.py makemigrations --check --dry-run`. Two agents can both
land `0002_*`; renumber the loser.

**Tests.** Never a bare `manage.py test`, never `--parallel`:
`.venv/bin/python manage.py test construction_projects`. Every agent shares one
`test_factory` database — give this module's runs their own `TEST NAME`.

**Commits.** Explicit pathspec:
`git commit -- construction_projects docs/construction_projects config/settings.py config/urls.py`.
Never `git add -A`. If a push is rejected, `git pull --rebase && git push` again —
never cherry-pick onto `origin/main`.

**Dependencies.** This module needs none, on either side.

**Never a bare `manage.py migrate`.** It applies every pending migration in the
tree, including another agent's half-finished one. Name the app:
`manage.py migrate construction_projects`. This is the migration equivalent of
the explicit-pathspec commit rule.

---

## 9. What is deliberately left out, and what it would cost later

| Left out | If it is ever wanted |
|---|---|
| Contractor billing (RA bills, retention, TDS) | A separate module reading this one. Do not grow it inside `Expense`. |
| Work breakdown / BOQ | A `ProjectTask` table with an FK from `Expense`. One migration, no redesign. |
| Tagging gate material to a project | A join table against `construction_gatein.ConstructionGateEntry` — never a new field on that app's model, which belongs to another agent. |
| SAP posting | `Expense` gains a nullable `sap_reference`. Worth adding the column in Phase 2 if SAP is likely; it is free now and a migration on a busy table later. |
| Cost rates for own labour | `cost_master.CostRate` already holds them. Resolve, never duplicate — `factory_expense` spent three migrations undoing its own copy. |
| Multi-level approval | One approver is enough at this size. A second level is a `min_amount` on a small matrix table, added when somebody asks. |

The shape above is chosen so that each of those is an addition, not a rewrite.
