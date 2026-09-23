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
| `FuelEntry` | One filling. The row that gets written every day. |
| `ServiceEntry` | One service or repair bill. |
| `VehicleDocument` | Insurance, PUC, fitness, permit, road tax — held for the expiry date. |

`FleetPermission` is an unmanaged sentinel: no table, its migration only mints
the four permission rows.

## Approval

`FuelEntry` and `ServiceEntry` both extend `ApprovableEntry`. Every entry is
`PENDING` when written and only an `APPROVED` one is counted as spend — the
summaries in `services.py` filter on it, and the pending count is reported
separately so nothing is quietly left out.

An approved entry is frozen: `PATCH` and `DELETE` are refused until it is sent
back. Entering (`can_add_fleet_expense`) and approving
(`can_approve_fleet_expense`) are separate rights on purpose.

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
