"""Every permission EXIM defines, carried over under the ``exim`` label.

EXIM (the import/export system on its own server) is being brought into this
project one module at a time. Its users arrive first, before any of its
screens, and they arrive holding the rights they hold in EXIM today — so every
one of those rights has to exist here from the start, on ``EximPermission``.

The codenames are EXIM's own, unchanged: ``tank.view_tankdata`` in EXIM is
``exim.view_tankdata`` here. They are unique across EXIM's apps, so the one
label holds them all, and ``import_exim_users`` maps a grant by nothing more
than its source app and codename.

The list is EVERYTHING EXIM defines, not only what its views check. EXIM's
frontend decides which pages a user sees from all of their permissions, grouped
by model name, so a right no backend view checks can still be what shows a
page. Generated from EXIM's models.py files on 2026-09-26: the four default
rights of each model, then its Meta.permissions. EXIM's user-management rights
(``accounts.add_user`` and so on) are deliberately absent — who can manage logins
is this project's own question.

As a module moves across, its models declare ``default_permissions = ()`` and
check these codenames, so a grant made today keeps working when the screen it
opens arrives.
"""

#: (EXIM app label, codename, name). The app label is where the right lives in
#: EXIM, and is what a grant read from EXIM's database is matched against.
CATALOGUE = [
    # --- accounts ---
    ('accounts', 'view_exim_rates', 'Can see Exim Rates'),
    ('accounts', 'view_director_report', 'Can See Director Report'),
    ('accounts', 'add_opening_rate', 'Can Add Opening Rate'),
    ('accounts', 'view_bank_accounts', 'Can View bank Accounts'),
    ('accounts', 'view_bank_closing', 'Can View bank closing'),
    ('accounts', 'view_customer_balance_sheet', 'Can View Customer Balance Sheet'),
    ('accounts', 'view_customer_outstanding', 'Can view Customer Outstanding'),
    ('accounts', 'view_customer_ledger', 'Can view Customer Ledger'),
    ('accounts', 'view_customer_aging', 'Can view Customer Aging'),
    ('accounts', 'view_open_ars', 'Can view Open ARs'),
    ('accounts', 'view_vendor_outstanding', 'Can view Vendor Outstanding'),
    ('accounts', 'view_vendor_ledger', 'Can view Vendor Ledger'),
    ('accounts', 'view_open_aps', 'Can view Open APs'),
    ('accounts', 'view_open_pos', 'Can view Open POs'),
    ('accounts', 'view_finance_dashboard', 'Can view Finance Dashboard'),
    ('accounts', 'view_bank_ledger', 'Can view Bank Ledger'),
    # --- contracts ---
    ('contracts', 'add_domesticreports', 'Can add domestic reports'),
    ('contracts', 'change_domesticreports', 'Can change domestic reports'),
    ('contracts', 'delete_domesticreports', 'Can delete domestic reports'),
    ('contracts', 'view_domesticreports', 'Can view domestic reports'),
    ('contracts', 'add_domesticcontractdetails', 'Can add domestic contract details'),
    ('contracts', 'change_domesticcontractdetails', 'Can change domestic contract details'),
    ('contracts', 'delete_domesticcontractdetails', 'Can delete domestic contract details'),
    ('contracts', 'view_domesticcontractdetails', 'Can view domestic contract details'),
    # --- daily_price ---
    ('daily_price', 'add_dailyprice', 'Can add daily price'),
    ('daily_price', 'change_dailyprice', 'Can change daily price'),
    ('daily_price', 'delete_dailyprice', 'Can delete daily price'),
    ('daily_price', 'view_dailyprice', 'Can view daily price'),
    ('daily_price', 'fetch_daily_price', 'Can fetch Daily Prices'),
    ('daily_price', 'view_daily_price_graph', 'Can see daily price graph'),
    ('daily_price', 'add_jivorates', 'Can add jivo rates'),
    ('daily_price', 'change_jivorates', 'Can change jivo rates'),
    ('daily_price', 'delete_jivorates', 'Can delete jivo rates'),
    ('daily_price', 'view_jivorates', 'Can view jivo rates'),
    ('daily_price', 'fetch_jivo_rates', 'Can fetch jivo rates'),
    # --- license ---
    ('license', 'add_advancelicenseheaders', 'Can add advance license headers'),
    ('license', 'change_advancelicenseheaders', 'Can change advance license headers'),
    ('license', 'delete_advancelicenseheaders', 'Can delete advance license headers'),
    ('license', 'view_advancelicenseheaders', 'Can view advance license headers'),
    ('license', 'add_advancelicenseimportlines', 'Can add advance license import lines'),
    ('license', 'change_advancelicenseimportlines', 'Can change advance license import lines'),
    ('license', 'delete_advancelicenseimportlines', 'Can delete advance license import lines'),
    ('license', 'view_advancelicenseimportlines', 'Can view advance license import lines'),
    ('license', 'add_advancelicenseexportlines', 'Can add advance license export lines'),
    ('license', 'change_advancelicenseexportlines', 'Can change advance license export lines'),
    ('license', 'delete_advancelicenseexportlines', 'Can delete advance license export lines'),
    ('license', 'view_advancelicenseexportlines', 'Can view advance license export lines'),
    ('license', 'add_dfialicenseheader', 'Can add dfia license header'),
    ('license', 'change_dfialicenseheader', 'Can change dfia license header'),
    ('license', 'delete_dfialicenseheader', 'Can delete dfia license header'),
    ('license', 'view_dfialicenseheader', 'Can view dfia license header'),
    ('license', 'add_dfialicenseexportlines', 'Can add dfia license export lines'),
    ('license', 'change_dfialicenseexportlines', 'Can change dfia license export lines'),
    ('license', 'delete_dfialicenseexportlines', 'Can delete dfia license export lines'),
    ('license', 'view_dfialicenseexportlines', 'Can view dfia license export lines'),
    ('license', 'add_dfialicenseimportlines', 'Can add dfia license import lines'),
    ('license', 'change_dfialicenseimportlines', 'Can change dfia license import lines'),
    ('license', 'delete_dfialicenseimportlines', 'Can delete dfia license import lines'),
    ('license', 'view_dfialicenseimportlines', 'Can view dfia license import lines'),
    # --- planning ---
    ('planning', 'add_planningupload', 'Can add planning upload'),
    ('planning', 'change_planningupload', 'Can change planning upload'),
    ('planning', 'delete_planningupload', 'Can delete planning upload'),
    ('planning', 'view_planningupload', 'Can view planning upload'),
    ('planning', 'add_planningrow', 'Can add planning row'),
    ('planning', 'change_planningrow', 'Can change planning row'),
    ('planning', 'delete_planningrow', 'Can delete planning row'),
    ('planning', 'view_planningrow', 'Can view planning row'),
    # --- rates ---
    ('rates', 'add_commoditymargin', 'Can add commodity margin'),
    ('rates', 'change_commoditymargin', 'Can change commodity margin'),
    ('rates', 'delete_commoditymargin', 'Can delete commodity margin'),
    ('rates', 'view_commoditymargin', 'Can view commodity margin'),
    ('rates', 'add_marketrates', 'Can add market rates'),
    ('rates', 'change_marketrates', 'Can change market rates'),
    ('rates', 'delete_marketrates', 'Can delete market rates'),
    ('rates', 'view_marketrates', 'Can view market rates'),
    ('rates', 'add_packingmargins', 'Can add packing margins'),
    ('rates', 'change_packingmargins', 'Can change packing margins'),
    ('rates', 'delete_packingmargins', 'Can delete packing margins'),
    ('rates', 'view_packingmargins', 'Can view packing margins'),
    ('rates', 'add_basicrates', 'Can add basic rates'),
    ('rates', 'change_basicrates', 'Can change basic rates'),
    ('rates', 'delete_basicrates', 'Can delete basic rates'),
    ('rates', 'view_basicrates', 'Can view basic rates'),
    ('rates', 'add_packsize', 'Can add pack size'),
    ('rates', 'change_packsize', 'Can change pack size'),
    ('rates', 'delete_packsize', 'Can delete pack size'),
    ('rates', 'view_packsize', 'Can view pack size'),
    ('rates', 'add_packrates', 'Can add pack rates'),
    ('rates', 'change_packrates', 'Can change pack rates'),
    ('rates', 'delete_packrates', 'Can delete pack rates'),
    ('rates', 'view_packrates', 'Can view pack rates'),
    # --- sap_sync ---
    ('sap_sync', 'add_synclogs', 'Can add sync logs'),
    ('sap_sync', 'change_synclogs', 'Can change sync logs'),
    ('sap_sync', 'delete_synclogs', 'Can delete sync logs'),
    ('sap_sync', 'view_synclogs', 'Can view sync logs'),
    ('sap_sync', 'sync_balance_sheet', 'Can sync Balance Sheet'),
    ('sap_sync', 'sync_open_grpos', 'Can sync open GRPOS'),
    ('sap_sync', 'sync_inventory', 'Can sync inventory'),
    ('sap_sync', 'add_rmproducts', 'Can add rm products'),
    ('sap_sync', 'change_rmproducts', 'Can change rm products'),
    ('sap_sync', 'delete_rmproducts', 'Can delete rm products'),
    ('sap_sync', 'view_rmproducts', 'Can view rm products'),
    ('sap_sync', 'sync_rm', 'Can sync RM'),
    ('sap_sync', 'add_fgproducts', 'Can add fg products'),
    ('sap_sync', 'change_fgproducts', 'Can change fg products'),
    ('sap_sync', 'delete_fgproducts', 'Can delete fg products'),
    ('sap_sync', 'view_fgproducts', 'Can view fg products'),
    ('sap_sync', 'sync_fg', 'Can sync FG'),
    ('sap_sync', 'add_party', 'Can add party'),
    ('sap_sync', 'change_party', 'Can change party'),
    ('sap_sync', 'delete_party', 'Can delete party'),
    ('sap_sync', 'view_party', 'Can view party'),
    ('sap_sync', 'sync_party', 'Can sync party'),
    ('sap_sync', 'add_domesticcontracts', 'Can add domestic contracts'),
    ('sap_sync', 'change_domesticcontracts', 'Can change domestic contracts'),
    ('sap_sync', 'delete_domesticcontracts', 'Can delete domestic contracts'),
    ('sap_sync', 'view_domesticcontracts', 'Can view domestic contracts'),
    ('sap_sync', 'sync_po', 'Can sync PO based on GRPO'),
    # --- stock ---
    ('stock', 'add_stockstatus', 'Can add stock status'),
    ('stock', 'change_stockstatus', 'Can change stock status'),
    ('stock', 'delete_stockstatus', 'Can delete stock status'),
    ('stock', 'view_stockstatus', 'Can view stock status'),
    ('stock', 'view_vehicle_report', 'Can view Vehicle Report'),
    ('stock', 'add_stockstatusupdatelog', 'Can add stock status update log'),
    ('stock', 'change_stockstatusupdatelog', 'Can change stock status update log'),
    ('stock', 'delete_stockstatusupdatelog', 'Can delete stock status update log'),
    ('stock', 'view_stockstatusupdatelog', 'Can view stock status update log'),
    ('stock', 'add_debitentry', 'Can add debit entry'),
    ('stock', 'change_debitentry', 'Can change debit entry'),
    ('stock', 'delete_debitentry', 'Can delete debit entry'),
    ('stock', 'view_debitentry', 'Can view debit entry'),
    ('stock', 'add_stockstatuschangesession', 'Can add stock status change session'),
    ('stock', 'change_stockstatuschangesession', 'Can change stock status change session'),
    ('stock', 'delete_stockstatuschangesession', 'Can delete stock status change session'),
    ('stock', 'view_stockstatuschangesession', 'Can view stock status change session'),
    ('stock', 'add_stockstatusfieldlog', 'Can add stock status field log'),
    ('stock', 'change_stockstatusfieldlog', 'Can change stock status field log'),
    ('stock', 'delete_stockstatusfieldlog', 'Can delete stock status field log'),
    ('stock', 'view_stockstatusfieldlog', 'Can view stock status field log'),
    ('stock', 'add_contractualhistory', 'Can add contractual history'),
    ('stock', 'change_contractualhistory', 'Can change contractual history'),
    ('stock', 'delete_contractualhistory', 'Can delete contractual history'),
    ('stock', 'view_contractualhistory', 'Can view contractual history'),
    ('stock', 'add_dashboardsnapshot', 'Can add dashboard snapshot'),
    ('stock', 'change_dashboardsnapshot', 'Can change dashboard snapshot'),
    ('stock', 'delete_dashboardsnapshot', 'Can delete dashboard snapshot'),
    ('stock', 'view_dashboardsnapshot', 'Can view dashboard snapshot'),
    ('stock', 'add_dashboardorder', 'Can add dashboard order'),
    ('stock', 'change_dashboardorder', 'Can change dashboard order'),
    ('stock', 'delete_dashboardorder', 'Can delete dashboard order'),
    ('stock', 'view_dashboardorder', 'Can view dashboard order'),
    # --- tank ---
    ('tank', 'add_tankitem', 'Can add tank item'),
    ('tank', 'change_tankitem', 'Can change tank item'),
    ('tank', 'delete_tankitem', 'Can delete tank item'),
    ('tank', 'view_tankitem', 'Can view tank item'),
    ('tank', 'add_tankdata', 'Can add tank data'),
    ('tank', 'change_tankdata', 'Can change tank data'),
    ('tank', 'delete_tankdata', 'Can delete tank data'),
    ('tank', 'view_tankdata', 'Can view tank data'),
    ('tank', 'view_itemwise_average', 'Can See Item Average'),
    ('tank', 'add_tanklog', 'Can add tank log'),
    ('tank', 'change_tanklog', 'Can change tank log'),
    ('tank', 'delete_tanklog', 'Can delete tank log'),
    ('tank', 'view_tanklog', 'Can view tank log'),
]

#: Codenames as EximPermission declares them.
PERMISSIONS = [(codename, name) for _app, codename, name in CATALOGUE]

#: (EXIM app label, codename) -> codename here.
BY_SOURCE = {(app, codename): codename for app, codename, _name in CATALOGUE}
