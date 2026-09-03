"""The one map between engine cost categories and central Cost Master codes.

Both the cost engines (blowing, production_execution) and the
``import_scattered_costs`` command import from here, so a renamed or mistyped
code is a KeyError/import-time mismatch rather than a silently-zero cost line.

Each entry: category -> (code, name, default_basis, is_credit, description).
The code is the stable key consumers resolve rates by; never reuse one for a
different cost.
"""

BLOWING_COST_TYPES = {
    'OPERATOR': ('blowing-operator', 'Blowing — Operator', 'PER_PERSON_DAY', False,
                 'Rate per operator per day.'),
    'LABOUR': ('blowing-labour', 'Blowing — Labour (contract + own)', 'PER_PERSON_DAY', False,
               'Rate per worker per day.'),
    'ELECTRICITY_MACHINE': ('blowing-electricity-machine', 'Blowing — Electricity (machine)',
                            'PER_UNIT', False, 'Metered machine electricity.'),
    'ELECTRICITY_UTILITY': ('blowing-electricity-utility', 'Blowing — Electricity (utility)',
                            'PER_UNIT', False, 'Metered utility electricity.'),
    'PACKING': ('blowing-packing', 'Blowing — Packing', 'PER_BOTTLE', False,
                'Packing cost per good bottle.'),
    'SCRAP_RECOVERY': ('blowing-scrap-recovery', 'Blowing — Scrap Recovery', 'PER_BOTTLE', True,
                       'Credit: value recovered per rejected bottle sold as scrap.'),
    'WASTAGE': ('blowing-wastage', 'Blowing — Wastage', 'PER_BOTTLE', False,
                'Rejected preform value (usually derived, not a set rate).'),
    'BENCHMARK_BLOWING_PER_BOTTLE': ('blowing-benchmark-per-bottle',
                                     'Blowing — Industry Benchmark / Bottle', 'PER_BOTTLE', False,
                                     'Editable industry benchmark, not a cost.'),
}

# NOTE: MATERIAL ('prod-material') is deliberately here for the import (legacy
# rows are preserved as data), but the run-costing engine must NEVER apply it —
# material cost comes from the run's own BOM snapshot, and a rate on top would
# double-count it (and violate the one-cost-line-per-category constraint).
PRODUCTION_COST_TYPES = {
    'MATERIAL': ('prod-material', 'Production — Material', 'PER_CASE', False,
                 'Informational only: run material cost is priced off the BOM '
                 'snapshot, never off this rate.'),
    'ELECTRICITY_VARIABLE': ('prod-electricity-variable', 'Production — Electricity (usage)',
                             'PER_UNIT', False, ''),
    'ELECTRICITY_FIXED': ('prod-electricity-fixed', 'Production — Electricity (fixed)',
                          'PER_DAY', False, ''),
    'LABOUR': ('prod-labour', 'Production — Labour', 'PER_PERSON_DAY', False, ''),
    'MANPOWER_SALARIED': ('prod-salary', 'Production — Salary', 'PER_MONTH', False, ''),
    'LUBRICATION': ('prod-lubrication', 'Production — Lubrication', 'PER_CASE', False, ''),
    'LAB_CHEMICALS': ('prod-lab-chemicals', 'Production — Lab Chemicals', 'PER_CASE', False, ''),
    'BATCH_CODING': ('prod-batch-coding', 'Production — Batch Coding', 'PER_CASE', False, ''),
    'MAINTENANCE': ('prod-maintenance', 'Production — Maintenance', 'PER_MONTH', False, ''),
    'WATER': ('prod-water', 'Production — Water', 'PER_CASE', False, ''),
    'OVERHEAD': ('prod-overhead', 'Production — Overhead', 'PER_MONTH', False, ''),
    'WASTE_RECOVERY': ('prod-waste-recovery', 'Production — Waste Recovery', 'PER_CASE', True,
                       'Credit: waste sale recovery.'),
    'OTHER': ('prod-other', 'Production — Other', 'PER_CASE', False, ''),
}

# category -> central code, for the engines' bulk resolves.
BLOWING_CODE_MAP = {category: meta[0] for category, meta in BLOWING_COST_TYPES.items()}
PRODUCTION_CODE_MAP = {category: meta[0] for category, meta in PRODUCTION_COST_TYPES.items()}

# production_execution's PER_UNIT choice is labelled "Per Case"; the central
# master stores that as PER_CASE. One direction is declared, the other derived.
PRODUCTION_BASIS_TO_CENTRAL = {
    'PER_UNIT': 'PER_CASE',
    'PER_PERSON_DAY': 'PER_PERSON_DAY',
    'PER_DAY': 'PER_DAY',
    'PER_HOUR': 'PER_HOUR',
    'PER_MONTH': 'PER_MONTH',
}
CENTRAL_BASIS_TO_PRODUCTION = {v: k for k, v in PRODUCTION_BASIS_TO_CENTRAL.items()}
