# Company Vehicles

The vehicles the company **owns** — the trucks, the cars, the Eeco, the scooty
— and what they cost to run.

Not to be confused with `vehicle_management`, which is the **gate's** register:
a row per outside truck that arrives, a transporter on every one, and nothing
about running cost. Nothing here writes to that app, or to `cash_book`,
`factory_expense` or any accounts module. `payment_mode` records how a bill was
paid and stops there.

## Tables

| Model | What it holds |
|---|---|
| `FleetVehicle` | The vehicle. Four required fields: number, category, fuel, status. |
| `DailyReading` | One meter reading on one day. One row per vehicle per day. |
| `FuelEntry` | One filling. The row that gets written every day. No approval. |
| `ServiceEntry` | One service or repair bill. |
| `VehicleDocument` | Insurance, PUC, fitness, permit, road tax — held for the expiry date. |

`FleetPermission` is an unmanaged sentinel: no table, its migration only mints
the four permission rows.

## Approval

`ServiceEntry` alone extends `ApprovableEntry`. A workshop bill is `PENDING`
when written and only an `APPROVED` one is counted as spend — `services.py`
filters on it, and the pending count is reported separately so nothing is
quietly left out.

**Fuel has no approval.** A filling is a pump slip for a few thousand rupees,
entered daily, and passing each one was work with no decision in it. A filling
counts the moment it is recorded, and `created_by` says who entered it.

An approved bill is frozen: `PATCH` and `DELETE` are refused until it is sent
back. A fuel entry, having no approval, stays editable. Entering
(`can_add_fleet_expense`) and approving (`can_approve_fleet_expense`) are
separate rights on purpose.

## Mileage

The one number here nobody can check by eye, so it is computed rather than
typed. `services.recalculate_fuel_metrics` runs over a whole vehicle after any
create, edit or delete of a fuel entry, because all three move the row its
neighbours are measured against.

Two rules:

* **Streams.** Each fuel is its own chain. A dual-fuel Eeco's petrol fillings
  and its CNG fillings are never averaged together — km/litre and km/kg are
  different units.
* **Full tank to full tank.** Mileage is the distance between two brimmed tanks
  divided by everything put in after the first of them. A part fill gets no
  mileage of its own, but its litres still count towards the next full tank's
  figure.

Entries are walked in date order, not meter order, so a replaced odometer
cannot reorder history; where the meter goes backwards the distance is left
blank rather than guessed.

## The fuel form's three conveniences

All three are in `FuelEntryWriteSerializer`, and all three exist because of
what a pump slip looks like:

1. **Any two of quantity, rate and amount.** The third is worked out.
2. **A meter below the last reading is a question.** Accepted once
   `odometer_note` says why — meters get replaced and do break.
3. **The same bill twice is queried once.** `confirm_duplicate` is how the page
   says it meant it.

## The running log

`running-log/` is the day-wise history. With `vehicle` it returns a row per day
for that vehicle; without, a row per vehicle for the window — the fleet-wide
answer to "which vehicle ran how much".

Three tables hold meter readings: `DailyReading`, `FuelEntry` and
`ServiceEntry`. A person at a pump is already writing the meter down, so the
log merges all three and takes the day's **highest** reading as its closing
figure. Nothing has to be typed twice.

Distance is only claimed where two readings bracket it. Where the previous
reading is older than the day before, the row carries `covers_days` so the
page can say the figure is a stretch rather than a day's running — a truck
nobody read on Sunday did not do 600 km on Monday. A day with no reading
comes back as a row of nulls: the gaps are the point.

`POST daily-readings/` is an **upsert** on (vehicle, date). Re-entering a day
overwrites it, so correcting a typo is the same action as entering it.

## Files

Bill photos, document scans and vehicle photos are served by
`FleetAttachmentAPI` at `attachments/<kind>/<pk>/`, never at their `/media/`
path — the endpoint is permission checked, and the media path is neither
authenticated nor on the same host as the SPA.

## Setting it up on a live database

1. `manage.py migrate company_vehicle` — creates the tables and the four
   permissions.
2. Grant the permissions to groups in the admin. Nobody sees the module in the
   sidebar until they hold at least one of them.
3. Add the vehicles (four boxes each), then their documents.
