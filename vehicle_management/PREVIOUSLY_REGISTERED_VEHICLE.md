# Previously Registered Vehicle

A view-first step in **Dispatch → Vehicle Linking**: before creating a vehicle,
look one up by registration number to see everything already captured about it.

## Flow (as agreed)
- A **"Previously Registered Vehicle"** button on the Vehicle Linking page opens a
  **separate page** (it does not open automatically).
- Enter/scan a registration number → the page shows the vehicle's captured details.
- **View-first, then create:** the create-vehicle dialog only opens from this page
  when the number **isn't** found ("Register this vehicle").

## What the page shows (real data)
- **Vehicle:** type, capacity (t), **dimensions (L×W×H metres)**, transporter
  (name/contact/mobile/GSTIN), registered-on date, **last factory visit date**,
  total visit count.
- **Driver (most recent):** name, mobile, license, ID proof, and **photo**.
- **Previous visits:** every `VehicleEntry` gate visit — entry no, date, type,
  driver, status, photo count.
- **Photos:** driver photo + per-visit gate attachments + dispatch **truck photos**.

## Dimensions
The `Vehicle` model gained `length_m`, `width_m`, `height_m` (metres, optional).
They're captured in the create/edit vehicle dialog ("Dimensions (metres) — optional")
and shown on the page. Existing vehicles show blank until filled — the page labels
them "Not captured yet" when absent.

## API
`GET /vehicle-management/vehicles/by-number/{vehicle_number}/history/`
(case-insensitive; gated on `dispatch_plans.can_link_dispatch_vehicle`). Returns
`{found, vehicle, driver, last_visit_date, visit_count, visits[], photos[]}`, or
`{found: false, vehicle_number}` when not registered. Built by
`vehicle_history_service.build_vehicle_history` from the reverse relations that all
FK back to the single `Vehicle` row (`driver_gate_entries`, `sales_dispatch_gate_outs`).

## Frontend
- Route `/dispatch/vehicle-linking/previously-registered` (gated by `LINK_VEHICLE`).
- Page `PreviouslyRegisteredVehiclePage`; button added to the Vehicle Linking header.
- Reuses `CreateVehicleDialog` (now with dimension inputs) for the not-found path.
