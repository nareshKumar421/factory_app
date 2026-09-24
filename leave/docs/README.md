# Leave

Who asked to be away, who allowed it, and what the attendance sheet was told.

## The shape of it

```
employee_hierarchy                        leave                        attendance
  Employee.reporting_manager ──routing──▶  LeaveRequest
  Employee.hierarchy_path                    └── LeaveRequestDay ──┐
  Employee.user (the login)                  └── LeaveApproval     │
                                                  (append-only)    │ projection
                                                                   ▼
                                                        override_status(ON_LEAVE)
                                                                   │
                                                                   ▼
                                                    DailyAttendance.effective_status
                                                    (machine_status NEVER touched)
```

Three modules, and the arrows only ever point one way. `leave` reads the
reporting tree and writes to the attendance sheet; neither of those knows this
module exists.

## The reporting tree decides, not the permission

This is the **first module here to authorise by position in the org**.
`labour_request`, `budget_approvals` and `invoice_approval` all work the other
way — hold the grant and you can decide anything. That is right for a budget
and wrong for leave, where the question is not "are you an approver?" but "are
you *their* approver?".

A decision therefore needs both a grant *and* a position:

| Authority | Who | How it is established |
|---|---|---|
| `manager` | the applicant's direct manager | `Employee.reporting_manager` |
| `skip_level` | anybody further up the same chain | one `hierarchy_path` prefix match |
| `hr` | `can_decide_any_leave` | the only grant that reaches outside a tree |

Skip-level is allowed on purpose. Thirteen people carry the reporting lines for
249 employees on the live data; if the one manager in a chain is away, their
team could not get leave approved at all. The materialised path makes "is this
person above that one?" a single string comparison, so allowing it costs
nothing and refusing it would strand people.

**Nobody approves their own leave**, however high they sit. Checked before
anything else, and it holds even for `can_decide_any_leave`.

All of it lives in `routing.py` — one file to read to know who may decide what.

## Approved leave reaches the sheet, and never overwrites the machine

`machine_status` is not written. Ever. It is what the punching machine
recorded, it is immutable by design, and payroll disputes turn on being able to
ask months later what the machine actually said.

So `projection.py` does not touch attendance's tables. It calls
`attendance.services.override_status()` — the same door a human correction goes
through — and inherits three things for free: the machine columns are left
alone, the mandatory reason is enforced, and an `AttendanceOverrideLog` row is
written.

A full day becomes `ON_LEAVE`. A half day stays `HALF_DAY`, because the person
genuinely was at the gate for half of it and the machine will have seen them.

### The timing problem

Leave is approved *in advance*; `DailyAttendance` rows do not exist until
`sync_biometric_attendance` has run for that date. So projection happens twice:

* at approval, for dates whose row already exists (today and the past);
* afterwards, by a sweep, for everything else.

`is_projected` is per **day**, not per request, precisely because a request
spanning a week is projected one date at a time as the sync reaches them.

**Run the sweep after every sync:**

```bash
python manage.py sync_biometric_attendance --days 2
python manage.py project_approved_leave       --days 2
```

Suggested cron, following the sync's own nightly slot:

```cron
30 2 * * *  cd /path/to/factory_app \
  && ./venv/bin/python manage.py sync_biometric_attendance --days 2 --quiet-progress \
  && ./venv/bin/python manage.py project_approved_leave --days 2
```

Idempotent over any range, exactly like the sync it follows.

### Cancelling does not blindly revert

Between the approval and the cancellation, HR may have corrected the same day
for an unrelated reason. The reversal only acts on a day still carrying
`APPROVED_LEAVE`, and reports the rest as left alone. Silently undoing somebody
else's correction is worse than leaving a stale one they can see.

## The prerequisite: logins must be linked to employees

`Employee.user` is the only thing that answers "which person is signed in?".
Measured on the live database when this module was written: **249 employees,
128 logins, 0 links** — and no way to derive one, because no employee carries an
email and no login carries a matching `employee_code`.

Nothing self-service works until that is fixed:

```bash
# a sheet from HR with employee_code and email columns
python manage.py link_employee_logins --file hr_logins.xlsx          # dry run
python manage.py link_employee_logins --file hr_logins.xlsx --commit

# the free matchers, for logins created properly later
python manage.py link_employee_logins --by-employee-code --by-email --commit
```

It refuses to move a link that already exists, and reports every ambiguity
rather than guessing. A wrong link is not a visible error — it is one person
applying for leave as another.

**Roughly half the workforce has no login at all.** That is why
`can_apply_leave_for_others` exists: the time office raises applications for
people who cannot. It is deliberately not paired with any decide grant —
entering somebody's leave and approving it are different jobs.

## What a day costs

Weekly offs and mandatory holidays are excluded when the day rows are built, so
Thursday-to-Monday is three days, not five.

* **Weekly offs** come from `settings.ATTENDANCE_WEEKLY_OFF_DAYS` — the *same*
  setting the attendance roll-up uses. Reading it from there rather than
  keeping a second list is the point: if the factory moves its off day, the
  sheet and the quota move together.
* **Holidays** come from `leave.Holiday`, mandatory ones only. A restricted
  holiday is offered, not taken, so somebody away that day is away.

## Quotas are enforced, and balances are computed

`annual_quota` is a **ceiling on what may be applied for**, checked when the
application is raised — not a figure reported afterwards. Charged per calendar
year, so a span crossing New Year spends from both years' entitlements rather
than taking next year's leave out of this year's balance.

`can_decide_any_leave` may exceed it deliberately (the view passes
`allow_overdraw`). Refusing HR outright would only push the record onto paper,
and compassionate leave past an entitlement is a real decision somebody makes.

`requires_document` is enforced the same way: a type that expects paperwork
refuses an application that arrives without a file. The endpoint accepts
multipart as well as JSON, because a `FileField` nothing can post to is
decoration.

The **balance** itself is entitlement minus what was taken, summed from the
approved day rows on every read. A stored counter would be a third copy to keep
in step with every approval, cancellation and partial decision — and the day it
drifts, nobody can say which number is wrong.

Pending days are reported *beside* used, not deducted from it: somebody with two
days left and two awaiting approval has not spent them yet, but cannot spend
them twice either. A quota of `0` means **untracked**, not none.


## Permissions

| Codename | Who |
|---|---|
| `can_apply_leave` | anyone with a linked employee record |
| `can_apply_leave_for_others` | the time office — covers those with no login |
| `can_view_team_leave` | managers, scoped to their subtree |
| `can_decide_leave` | approve/reject **within your subtree only** |
| `can_decide_any_leave` | HR — the only grant that leaves the tree |
| `can_cancel_approved_leave` | separate, because it unpicks a projection |
| `can_manage_leave_types` | the type and holiday masters |

```bash
python manage.py setup_leave_groups          # four groups, all empty by design
python manage.py setup_leave_groups --list
```

Groups start empty on purpose: a fresh login 403s until somebody decides which
of the four it is, and the alternative is a default that quietly grants
something.

## Endpoints

All under `/api/v1/leave/`.

| Route | Does |
|---|---|
| `GET/POST types/` | The leave types. Read by anyone in the module, edited by HR |
| `GET/POST holidays/` | The factory calendar |
| `GET requests/` | What your reach covers. `?mine=true`, `?status=`, `?from=&to=`, `?limit=` (capped at 500) |
| `POST requests/` | Apply. Omit `employee` to mean yourself |
| `GET requests/{id}/history/` | The append-only trail |
| `POST requests/{id}/approve/` | `{comment, only_dates?}` — `only_dates` is a partial approval |
| `POST requests/{id}/reject/` | `{comment}` — mandatory |
| `POST requests/{id}/withdraw/` | The applicant, before any decision |
| `POST requests/{id}/cancel/` | After approval; also reverts the projection |
| `GET pending/` | The approver's queue — only what they may act on |
| `GET pending/count/` | `COUNT(*)` for the sidebar badge, never the whole queue |
| `GET calendar/?from=&to=` | Who is out, over a window |
| `GET balance/?employee=&year=` | Quota, used, pending, available |
| `GET employees/?search=` | Who the time office may raise an application for |

Approve and reject are separate **paths**, not a field in the body: a payload
flag deciding between "allow" and "refuse" is one typo away from the wrong
outcome, and the two are separately auditable.

Every request row carries its own decision context — `can_decide`,
`my_authority`, `can_cancel`, `can_withdraw`. **The frontend gates its buttons
on those**, not on the permission, because authorisation depends on the tree and
a client that re-derives it is a client that gets it wrong.

## The screens

Under Organisation in the frontend: `/organization/leave` (my leave, with balance
cards and the history trail), `/organization/leave/approvals` (the queue, with a
sidebar badge fed by the count endpoint), `/organization/leave/calendar` (who is
out, with a per-day headcount) and `/organization/leave/settings` — the leave types
and the holiday calendar, gated on `can_manage_leave_types`. The old `/leave*` URLs
redirect there. Before that last
one existed both masters could only be reached through the Django admin, which
meant HR could not add a leave type without a developer.

## Tests

```bash
python manage.py test leave --settings=config.sqlite_test_settings
```

160 tests. The ones worth reading first are in `tests_projection.py`: every one
of them is really guarding the same invariant, that the machine's reading
survives untouched.
