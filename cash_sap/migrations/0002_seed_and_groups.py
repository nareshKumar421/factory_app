"""Seed the factory's routing and G/L item map, and create the two groups.

Every value below was read from SAP on 9 Oct 2026 (read-only): what USER22
keys for the factory imprest in JIVO_OIL and in JIVO_BEVERAGES since January,
and the SAP validation procedure both companies run.

ROUTING (only for the company coded ``JIVO_OIL`` and the branches it has;
names matched case-insensitively; a branch that already has a routing row is
left alone):

* **Oil, Common** -> JIVO_OIL, PO + GRPO, imprest vendor ORGV000465, SAP
  branch 2, warehouse BH-FA, Dim1 CANOLA, Dim3 Factory, Dim5 HR, IGST@0 /
  Exampt, A/P drafts off, a bill photo needed, one GRPO per voucher, A/P
  series hint ``HR_B`` (four A/P series fit branch 2 each month -- HR_B,
  HR_G, CNHR, HR_D -- and every imprest A/P since January is HR_B).
* **Beverage, Water** -> JIVO_BEVERAGES, the **same** PO + GRPO shape: its
  imprest vendor there is ORGV000245 (the same Arvinder Singh imprest; in
  JIVO_OIL that code is another person), SAP branch 2, warehouse BH-FA,
  Dim1 DRINKS (Beverage) or WATER (Water), Dim3 Factory, Dim5 HR, A/P series
  hint ``HR_B``. SAP's "Factory" budget needs branch 2 (rules 220010/200015).
* **Mart** (if present) -> a disabled, blank row.

G/L ITEM MAP, per SAP company (target account blank = the same code). Item
codes do NOT mean the same thing in the two companies -- CG0000015 is
REFRESHMENT in Oil but CO2 GAS in Beverages -- so each company has its own
rows. ``needs_review`` marks heads SAP shows booked both ways (as GRPO goods
and as A/P service lines) or seen only once.

GROUPS: "Cash Book SAP Poster" and "Cash Book SAP Settings", each with its
cash_sap right and the cash book's view right. Nobody is put in them. The same
groups are kept by ``manage.py setup_cash_sap_groups``.

Reverse removes what this seeded. Permissions are created first: on a fresh
database ``post_migrate`` has not run yet (the bug in grpo/0003).
"""

from django.db import migrations

COMPANY_CODE = "JIVO_OIL"
BEVERAGES_CODE = "JIVO_BEVERAGES"

OIL_ROUTING = dict(
    enabled=True, sap_company_code="JIVO_OIL", mode="PO_GRPO", vendor_code="ORGV000465",
    bpl_id=2, warehouse_code="BH-FA", ocr_code="CANOLA", ocr_code3="Factory", ocr_code5="HR",
    item_tax_code="IGST@0", service_tax_code="Exampt", ap_drafts_enabled=False,
    grpo_needs_attachment=True, grpo_grouping="NONE", ap_series_hint="HR_B",
)
BEVERAGE_ROUTING = dict(
    enabled=True, sap_company_code=BEVERAGES_CODE, mode="PO_GRPO", vendor_code="ORGV000245",
    bpl_id=2, warehouse_code="BH-FA", ocr_code="DRINKS", ocr_code3="Factory", ocr_code5="HR",
    item_tax_code="IGST@0", service_tax_code="Exampt", ap_drafts_enabled=False,
    grpo_needs_attachment=True, grpo_grouping="NONE", ap_series_hint="HR_B",
)
WATER_ROUTING = dict(BEVERAGE_ROUTING, ocr_code="WATER")
MART_ROUTING = dict(enabled=False)

#: branch name (lower case) -> seed
ROUTING_SEEDS = {
    "oil": OIL_ROUTING,
    "common": OIL_ROUTING,
    "beverage": BEVERAGE_ROUTING,
    "water": WATER_ROUTING,
    "mart": MART_ROUTING,
}

#: JIVO_OIL (G/L, head, item, needs_review) -- GRPO lines on ORGV000465, 2026
ITEM_HEADS = [
    ("5630004", "REFRESHMENT", "CG0000015", True),        # 50 GRPO lines, 48 A/P service lines
    ("5650001", "R&M OFFICE & BUILDING", "CG0000007", False),
    ("5650016", "R&M PLANT & MACHINERY", "CG0000003", False),
    ("5680015", "HOUSE KEEPING", "CG0000005", False),
    ("5680012", "PRINTING AND STATIONERY", "CG0000013", True),
    ("5630003", "STAFF WELFARE", "CG0000004", False),
    ("5680022", "COMPUTER AND HARDWARE", "CG0000008", False),
    ("5100018", "LAB & TESTING DIRECT EXP", "CG0000048", False),
    ("5100015", "CONSUMABLE/DIRECT EXP", "CG0000012", True),
    ("5100006", "PACKAGING MATERIALS EXP", "CG0000031", True),
]
SERVICE_HEADS = [
    ("5690002", "CONVEYANCE"),
    ("5670002", "LOADING & UNLOADING"),
    ("5650015", "FUEL"),
    ("5650002", "REPAIR VEHICLES"),
    ("5660005", "TOLL"),
    ("5680023", "POSTAGE & COURIER"),
    ("5680013", "LAB TESTING"),
    ("5640004", "SAMPLING"),
    ("5610003", "BANK CHARGES"),
]
NEVER_HEADS = [
    ("1101015", "SUNDRY DEBTORS STAFF"),
    ("5630001", "SALARY EXPENSE"),
]

#: JIVO_BEVERAGES -- GRPO lines and A/P service lines on ORGV000245, 2026
BEVERAGES_ITEM_HEADS = [
    ("5650016", "R&M PLANT & MACHINERY", "CG0000004", False),  # 27 GRPO lines, 8 service
    ("5650001", "R&M OFFICE & BUILDING", "CG0000007", False),
    ("5680015", "HOUSE KEEPING", "CG0000005", False),
    ("5680012", "PRINTING AND STATIONERY", "CG0000006", True),
    ("5630003", "STAFF WELFARE", "CG0000003", True),           # 2 GRPO lines, 5 service
    ("5100006", "PACKAGING MATERIALS EXP", "CG0000021", True),
]
BEVERAGES_SERVICE_HEADS = [
    ("5670002", "LOADING & UNLOADING"),
    ("5690002", "CONVEYANCE"),
    ("5630004", "REFRESHMENT"),                                # a service line in Beverages
    ("5680010", "CETP CHARGES"),
    ("5660005", "TOLL"),
    ("5680023", "POSTAGE & COURIER"),
    ("5650015", "FUEL"),
    ("5660002", "RENT"),
    ("5680028", "FREIGHT INWARD"),
    ("5650002", "REPAIR VEHICLES"),
    ("5610003", "BANK CHARGES"),
    ("5680013", "LAB TESTING"),
    ("5640004", "SAMPLING"),
]

POSTER_GROUP = "Cash Book SAP Poster"
SETTINGS_GROUP = "Cash Book SAP Settings"
GROUPS = {
    POSTER_GROUP: [("cash_book", "can_view_cash_book"), ("cash_sap", "can_post_cash_to_sap")],
    SETTINGS_GROUP: [("cash_book", "can_view_cash_book"), ("cash_sap", "can_manage_cash_sap_settings")],
}


def _rows(items, services, nevers):
    rows = [
        dict(gl_account_code=gl, gl_account_name=name, treatment="ITEM", item_code=item,
             needs_review=review)
        for gl, name, item, review in items
    ]
    rows += [dict(gl_account_code=gl, gl_account_name=name, treatment="SERVICE") for gl, name in services]
    rows += [dict(gl_account_code=gl, gl_account_name=name, treatment="NEVER") for gl, name in nevers]
    return rows


def _map_rows():
    """``{SAP company: [row, ...]}``."""
    return {
        COMPANY_CODE: _rows(ITEM_HEADS, SERVICE_HEADS, NEVER_HEADS),
        BEVERAGES_CODE: _rows(BEVERAGES_ITEM_HEADS, BEVERAGES_SERVICE_HEADS, NEVER_HEADS),
    }


def seed_routing_and_map(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    CashBranch = apps.get_model("cash_book", "CashBranch")
    Routing = apps.get_model("cash_sap", "CashBranchSapRouting")
    GLItemMap = apps.get_model("cash_sap", "GLItemMap")

    company = Company.objects.filter(code=COMPANY_CODE).first()
    if company is None:
        return

    for branch in CashBranch.objects.filter(company=company):
        seed = ROUTING_SEEDS.get((branch.name or "").strip().lower())
        if seed is None or Routing.objects.filter(branch=branch).exists():
            continue
        Routing.objects.create(branch=branch, **seed)

    for sap_company, rows in _map_rows().items():
        for row in rows:
            exists = GLItemMap.objects.filter(
                sap_company_code=sap_company, gl_account_code=row["gl_account_code"], is_active=True
            ).exists()
            if not exists:
                GLItemMap.objects.create(sap_company_code=sap_company, target_account_code="", **row)


def unseed_routing_and_map(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    Routing = apps.get_model("cash_sap", "CashBranchSapRouting")
    GLItemMap = apps.get_model("cash_sap", "GLItemMap")

    company = Company.objects.filter(code=COMPANY_CODE).first()
    if company is not None:
        names = list(ROUTING_SEEDS)
        for routing in Routing.objects.filter(branch__company=company).select_related("branch"):
            if (routing.branch.name or "").strip().lower() in names:
                routing.delete()
    for sap_company, rows in _map_rows().items():
        GLItemMap.objects.filter(
            sap_company_code=sap_company,
            gl_account_code__in=[row["gl_account_code"] for row in rows],
        ).delete()


def create_groups(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    for label in ("cash_book", "cash_sap"):
        create_permissions(global_apps.get_app_config(label), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for name, codes in GROUPS.items():
        group, _ = Group.objects.get_or_create(name=name)
        for app_label, codename in codes:
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is not None:
                group.permissions.add(permission)


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=list(GROUPS)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cash_sap", "0001_initial"),
        ("cash_book", "0014_a_voucher_can_be_signed_on_paper"),
        ("company", "0003_alter_usercompany_role"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_routing_and_map, unseed_routing_and_map, elidable=False),
        migrations.RunPython(create_groups, remove_groups, elidable=False),
    ]
