# Auto-Fetch BOM When Starting a Production Run

## Current Behavior

1. User creates a Production Run, optionally linking it to a SAP Production Order via `sap_doc_entry`
2. User **manually** passes `materials` list in the create request body
3. Materials are saved as `ProductionMaterialUsage` records
4. BOM components exist in SAP table `WOR1` but are **not auto-fetched** during run creation

## Desired Behavior

When a user creates/starts a Production Run and selects an **item (product)**, the system should **automatically fetch the BOM components** from SAP and populate `ProductionMaterialUsage` records — no manual material entry needed.

---

## Implementation Plan

### Step 1: New SAP Reader Method — Fetch BOM by Item Code

**File:** `production_execution/services/sap_reader.py`

Add a new method to `ProductionOrderReader` that fetches BOM components directly from SAP's BOM tables (`OITT` header + `ITT1` lines) based on an `ItemCode`:

```python
def get_bom_by_item_code(self, item_code: str) -> list:
    """
    Fetch BOM components for a finished good item from SAP.
    Uses OITT (BOM header) + ITT1 (BOM lines) tables.
    Falls back to WOR1 if a sap_doc_entry is provided.
    """
    schema = self.client.context.config['hana']['schema']
    sql = """
        SELECT
            T1."Code"      AS "ItemCode",
            T1."Name"      AS "ItemName",
            T1."Quantity"   AS "PlannedQty",
            T1."Uom"       AS "UomCode"
        FROM "{schema}"."OITT" T0
        INNER JOIN "{schema}"."ITT1" T1 ON T0."Code" = T1."Father"
        WHERE T0."Code" = '{item_code}'
    """.format(schema=schema, item_code=item_code)
    return self._execute(sql)
```

> **Note:** The exact SAP table/column names (`OITT`, `ITT1`, `Code`, `Father`, `Quantity`, etc.) must be verified against the actual SAP HANA schema. SAP B1 uses `OITT`/`ITT1` for BOMs; SAP S/4HANA may differ.

**Alternative approach — use existing WOR1 query:**
If the run is linked to a SAP Production Order (`sap_doc_entry`), we already have `get_production_order_detail()` which returns BOM components from `WOR1`. We can reuse this directly.

### Step 2: New Service Method — Auto-Populate Materials from BOM

**File:** `production_execution/services/production_service.py`

Add a method that fetches BOM and creates `ProductionMaterialUsage` records:

```python
def auto_populate_materials_from_bom(self, run: ProductionRun):
    """
    Fetch BOM components from SAP and create ProductionMaterialUsage records.
    Priority:
      1. If run has sap_doc_entry → fetch from WOR1 (production order components)
      2. Else if run has product (ItemCode) → fetch from OITT/ITT1 (item BOM)
    """
    from .sap_reader import ProductionOrderReader, SAPReadError

    reader = ProductionOrderReader(self.company_code)
    components = []

    if run.sap_doc_entry:
        # Fetch from production order components (WOR1)
        detail = reader.get_production_order_detail(run.sap_doc_entry)
        components = detail.get('components', [])
    elif run.product:
        # Fetch from item BOM (OITT/ITT1)
        components = reader.get_bom_by_item_code(run.product)

    if not components:
        return []

    materials = []
    for comp in components:
        mat = ProductionMaterialUsage.objects.create(
            production_run=run,
            material_code=comp.get('ItemCode', ''),
            material_name=comp.get('ItemName', ''),
            opening_qty=comp.get('PlannedQty', 0),
            issued_qty=comp.get('IssuedQty', 0),
            closing_qty=0,
            uom=comp.get('UomCode', ''),
        )
        materials.append(mat)

    return materials
```

### Step 3: Modify `create_run` to Auto-Fetch BOM

**File:** `production_execution/services/production_service.py`

Update the existing `create_run` method (line ~236) to auto-fetch BOM when no manual materials are provided:

```python
def create_run(self, data: dict, user) -> ProductionRun:
    # ... existing code to create the run ...

    # Create initial materials
    materials_data = data.get('materials', [])
    if materials_data:
        # User provided materials manually — use them as-is
        self.save_material_usage(run.id, materials_data)
    else:
        # No manual materials — auto-fetch from SAP BOM
        try:
            self.auto_populate_materials_from_bom(run)
        except Exception as e:
            logger.warning(f"Could not auto-fetch BOM for run {run.id}: {e}")
            # Non-blocking: run is still created, materials can be added later

    return run
```

### Step 4: New API Endpoint — Fetch BOM for an Item (Optional, for frontend preview)

**File:** `production_execution/views.py`

Add an endpoint so the frontend can preview BOM components before creating a run:

```
GET /api/production_execution/sap/bom/?item_code=<ItemCode>
GET /api/production_execution/sap/orders/<doc_entry>/components/   (already exists)
```

```python
class SAPItemBOMAPI(APIView):
    """Fetch BOM components for a given item code."""

    def get(self, request, company_code):
        item_code = request.query_params.get('item_code')
        if not item_code:
            return Response({"error": "item_code is required"}, status=400)

        reader = ProductionOrderReader(company_code)
        components = reader.get_bom_by_item_code(item_code)
        return Response({"item_code": item_code, "components": components})
```

**File:** `production_execution/urls.py`

```python
path('sap/bom/', views.SAPItemBOMAPI.as_view(), name='sap-item-bom'),
```

### Step 5: Add Serializer for BOM Response

**File:** `production_execution/serializers.py`

```python
class SAPBOMComponentSerializer(serializers.Serializer):
    ItemCode = serializers.CharField()
    ItemName = serializers.CharField()
    PlannedQty = serializers.FloatField()
    IssuedQty = serializers.FloatField(required=False, default=0)
    UomCode = serializers.CharField(allow_null=True)
```

---

## Data Flow (After Implementation)

```
Frontend                          Backend                              SAP HANA
───────                          ───────                              ────────
1. User selects Item/Product
   (or SAP Production Order)
        │
        ▼
2. POST /runs/  ──────────►  create_run()
   {                              │
     sap_doc_entry: 123,          │  No manual materials?
     line_id: 1,                  │
     date: "2026-03-28",          ▼
     product: "FG-001"       auto_populate_materials_from_bom()
   }                              │
                                  ├─ sap_doc_entry exists?
                                  │   YES → get_production_order_detail() ──► WOR1 table
                                  │   NO  → get_bom_by_item_code()        ──► OITT/ITT1
                                  │
                                  ▼
                             Create ProductionMaterialUsage records
                             (one per BOM component line)
                                  │
                                  ▼
3. Response  ◄──────────     Run created with materials auto-filled
   {
     id: 42,
     materials: [
       {material_code: "RM-001", material_name: "Sugar", opening_qty: 500, uom: "KG"},
       {material_code: "PKG-01", material_name: "5L Bottle", opening_qty: 1000, uom: "PCS"},
       ...
     ]
   }
```

---

## Files to Modify

| File | Change |
|------|--------|
| `production_execution/services/sap_reader.py` | Add `get_bom_by_item_code()` method |
| `production_execution/services/production_service.py` | Add `auto_populate_materials_from_bom()`, modify `create_run()` |
| `production_execution/views.py` | Add `SAPItemBOMAPI` endpoint (optional preview) |
| `production_execution/urls.py` | Add `/sap/bom/` route |
| `production_execution/serializers.py` | Add `SAPBOMComponentSerializer` |

## Edge Cases to Handle

1. **SAP connection failure** — Non-blocking; run is created, materials added later manually
2. **Item has no BOM in SAP** — Return empty components list, log a warning
3. **User provides manual materials AND sap_doc_entry** — Manual materials take priority (no auto-fetch)
4. **Duplicate materials** — If user later manually adds materials that overlap with auto-fetched ones, handle via update/replace logic
5. **BOM quantities vs actual usage** — `opening_qty` is set from BOM `PlannedQty`; `closing_qty` and `wastage_qty` are filled during/after production

## SAP Table Reference

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `OWOR` | Production Order Header | DocEntry, ItemCode, PlannedQty, Status |
| `WOR1` | Production Order Components (BOM lines) | DocEntry, ItemCode, ItemName, PlannedQty, IssuedQty, UomCode |
| `OITT` | BOM Header (Item → BOM mapping) | Code (= ItemCode of parent) |
| `ITT1` | BOM Lines (components) | Father (= OITT.Code), Code, Name, Quantity, Uom |
| `OITM` | Item Master | ItemCode, ItemName, InvntryUom |
