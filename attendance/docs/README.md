# Attendance

Who turned up, according to the punching machines — and who corrected that, and why.

## The shape of it

```
Biometrics (SQL Server, factory LAN)     Postgres (this app)
  punchtransfer          ─┐
    paycode = JWPL code   │  sync/      attendance_punchevent   (raw, unresolved)
    CombinedDatetime      ├─ agent ──▶  attendance_punchalias   (alias mirror)
    ipaddress = serial    │  (Windows)  attendance_punchsyncrun (did it run?)
  factory_codes          ─┘                        │
                                                   │ sync_biometric_attendance
                                                   ▼
                                         attendance_dailyattendance
                                           machine_status   (immutable)
                                           effective_status (what stands)
                                         attendance_attendanceoverridelog
                                           append-only: who/from/to/why
```

**The punch box is not reachable from the application server.** An agent on a
Windows machine inside the plant copies punches into the three `punch*` tables;
it lives in the companion `sync/` repository and is the only thing that talks to
SQL Server. `sync_biometric_attendance` then reads *those*, derives a status per
person per day, and writes the `machine_*` columns.

The agent resolves nothing — it copies rows. Every rule about what punches mean
stays here, so fixing a wrong alias repairs the punches already copied across
rather than only the ones that arrive afterwards. A human with
`can_override_attendance_status` can change `effective_status` through the API,
with a mandatory reason. **The two never overwrite each other**, which is what
lets the UI show "punch machine only" as a column toggle rather than as a
reconstruction.

## The join key is the JWPL code

`punchtransfer.paycode` holds the same `JWPL0593` that HR types into the
hierarchy workbook. This is why `employee_hierarchy` had to be re-imported with
JWPL employee codes first — see
`employee_hierarchy/management/commands/import_hierarchy_jwpl.py`. A wrong code
is not a visible error; it is somebody else's attendance under your name.

Coverage as measured on 2026-09-17: 216 of the 225 codes on the hierarchy
workbook punched within the previous 30 days. `TP###` codes are contract staff
and punch like anyone else (62 of 66). People with no code at all are imported
under a `NOCODE-####` code that visibly cannot match a punch.

## Four things about the source schema that cost accuracy if missed

1. **There is no IN/OUT column.** A punch is a bare timestamp. First of the day
   is the arrival, last is the departure. About **14% of person-days carry a
   single punch** — somebody who forgot to punch out — which is neither present
   nor absent, gets `MISSING_PUNCH`, and is the main thing the override exists
   to resolve.
2. **`status` means "transferred", not "attended".** It holds `New`/`Done`, the
   vendor ETL's own flag. The client drops the column so nobody reads it as an
   attendance status.
3. **Only one of the four punch tables is live.** `punchtransfer` runs to today.
   `punchtransfer_factory` stopped 2025-07-08, `punchtransfer_factory_sushil` a
   month earlier, and `punchtransfer_dsr` is field staff on fixed 11:00/17:30
   timings. Pointing at an archive is silent — everybody simply reads as absent
   — so the table is the `ATTENDANCE_PUNCH_TABLE` setting, not a literal.
4. **A few people punch under an alias.** `factory_codes` maps `fac####` codes
   to JWPL ones, and three people punch *only* under the alias. The client
   resolves aliases before matching; without it those three punch daily and read
   as absent daily.

`ipaddress` is a device serial (`NCD8244900570`), not an IP. Four readers are
currently in service.

## Deriving a status

| Punches that day | Status | Why |
|---|---|---|
| 0, on a weekly off | `WEEKLY_OFF` | Flagging it would bury real absences under ~300 false ones every Sunday |
| 0, otherwise | `ABSENT` | |
| 1 | `MISSING_PUNCH` | They were at the gate, so absent is a lie; nothing says how long they stayed, so present is a guess |
| 2+, span < `ATTENDANCE_HALF_DAY_MINUTES` | `HALF_DAY` | |
| 2+, span ≥ threshold | `PRESENT` | |

The span is **gate to gate**. It is not overtime and it is not productive hours;
no report should treat it as either.

### Known limitation: shifts that cross midnight

The roll-up is per calendar day, so a night worker in at 21:00 and out at 05:00
appears as two days with one punch each. Measured over 30 days that is 63
punches by 46 people — 0.3% of the total. Fixing it properly needs a shift
master the business does not keep, and guessing which of two plausible shift
patterns each person is on would be wrong *silently*. `MISSING_PUNCH` plus an
override is wrong *visibly*, and takes ten seconds to correct. Revisit if shift
data ever arrives.

## Running it

Two jobs now, in order. The agent on the Windows box (see `sync/README.md`)
brings punches across; this command turns them into the sheet.

```bash
# today and yesterday — the nightly job. A late punch-out lands after midnight,
# so a day is not final until the next one has started.
python manage.py sync_biometric_attendance --days 2

# backfill a range the agent has already covered
python manage.py sync_biometric_attendance --date-from 2026-09-01 --date-to 2026-09-17
```

Safe to re-run over any range: only the `machine_*` columns are refreshed, so a
re-sync repairs punch data without undoing anybody's correction.

**It refuses to run when the agent has not reported within
`ATTENDANCE_SYNC_STALE_HOURS` (default 36), and exits non-zero** so a scheduler
notices. This is the safety mechanism that replaced "exits non-zero when the box
is unreachable", and it matters more than the old one did: rolling up punches
that never arrived does not fail, it quietly writes `ABSENT` for three hundred
people, and payroll is run from the result. `--allow-stale` overrides it when
that is genuinely what you want.

Suggested cron on the app server, after the agent's own nightly run:

```cron
30 2 * * *  cd /path/to/factory_app && ./venv/bin/python manage.py sync_biometric_attendance --days 2 --quiet-progress
```

## Permissions

Viewing and correcting are separate grants, not a ladder.

| Codename | Who | What |
|---|---|---|
| `can_view_daily_attendance` | gate groups, HR | Read the sheet |
| `can_override_attendance_status` | HR only | Change a status away from the machine's |
| `can_sync_attendance` | HR | Pull punches on demand |
| `can_export_attendance` | gate groups, HR | Download the sheet |

Migration `0006` grants these to the groups that already hold the equivalent
rights. Correcting attendance contradicts a machine on a day that has closed,
and payroll is run from the result — that is an HR act, which is why the
override grant is deliberately short.

## Endpoints

All under `/api/v1/attendance/`.

| Route | Does |
|---|---|
| `GET daily/?date=` | One day, everybody. **Always returns both statuses on every row.** |
| `POST daily/{id}/override/` | `{status, reason_code, reason}` — reason mandatory |
| `POST daily/{id}/revert/` | Back to the machine's reading, also logged |
| `GET daily/{id}/history/` | Every change ever made to that day |
| `GET daily/summary/` | Counts, by machine reading *and* by what stands |
| `GET daily/reasons/` | The status and reason-code vocabulary |
| `GET daily/source_status/` | Is the punch data current, when did the agent last run |
| `POST daily/sync/` | Re-derive the sheet from stored punches (≤92 days) |
| `GET daily/export/` | .xlsx with both statuses side by side |
| `employees/` | The directory, **read-only** |
| `records/` | Manual photographed gate marks (fallback when the machine is down) |

The daily viewset is read-only as a viewset: a `PATCH` would skip the mandatory
reason and the audit trail, which is the entire point of the module.

`/attendance/employees/` used to be a writable master of its own. Two employee
masters for one workforce meant somebody could exist for attendance and not for
HR — and then never match their own punches. It now serves
`employee_hierarchy.Employee`, read-only.

## Deployment prerequisite

Nothing here talks to SQL Server any more — `pymssql` and the `ATTENDANCE_DB_*`
settings are gone from this repo. What this module needs instead is that the
agent is actually running, which is what `daily/source_status/` reports: it reads
the newest `PunchSyncRun` rather than probing anything.

That endpoint exists so the UI can tell "the factory was shut" from "the sync has
not run since Tuesday", which otherwise look identical on the sheet. Its
`reachable` key now means *the punch data is current*, and it carries
`last_agent_run` and `stale` beside it.

The agent's own prerequisites — SQL Server credentials, a route to Postgres, and
a least-privilege role — are documented in `sync/README.md`. They are deliberately
not in this repo's `.env`, so nothing here can reach the punch box even by
accident.
