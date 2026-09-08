"""
The chart as it stands today — the seed for a fresh install.

This is a starting point, not a source of truth: once seeded, the chart is
edited on the page (``org_chart.can_manage_org_chart``) and this list is never
consulted again. ``seed_org_chart`` refuses to overwrite a chart that already
has rows unless it is asked to.
"""

#: Heading of the chart.
DEFAULT_PLANT_NAME = "Oil Plant"
DEFAULT_PLANT_HEAD = "Gagan Veerji"

#: (department, head, [(section, subtitle, leader-L1, supported-by-L2, team-L3), ...])
DEFAULT_CHART = [
    (
        "Procurement",
        "Shunty Veerji",
        [
            ("OIL", "", ["Shunty Veerji"], ["Raspreet", "Lovepreet", "Gopi"], ["Team"]),
            ("Packing material", "", ["Ravinder Veerji"], [], ["Team"]),
        ],
    ),
    (
        "Production",
        "Kulbir Veerji",
        [
            # Storage runs twice — same section, different material, different people.
            ("Storage", "OIL", ["Vicky Veerji"], ["Sunil"], ["Team"]),
            ("Storage", "Packing material", ["Kulbir Veerji"], ["Shahrukh"], ["Team"]),
            ("Production — OIL", "", ["Vicky Veerji"], ["Gautam"], ["Team"]),
            ("Material shifting", "", ["Charanjit Singh"], ["Monu"], ["Team"]),
        ],
    ),
    (
        "Supplies",
        "Sandeep Veerji",
        [
            ("Warehouse", "", ["Sandeep Veerji"], ["Honey", "Tejinder"], []),
            ("Despatch", "Documentation", ["Sandeep Veerji"], ["Raj", "Priya"], []),
            ("Despatch", "Docking", ["Sandeep Veerji"], ["Virender Veerji"], ["Team"]),
            ("Transportation", "", ["Sandeep Veerji"], ["Tiwariji"], []),
        ],
    ),
    (
        "Gupta Down",
        "",
        [
            (
                "Operations",
                "",
                ["Prabhu Veerji"],
                ["Prince", "Gagan"],
                ["Arsh", "Santosh", "Jassi"],
            ),
            ("Audit", "", ["Sandeep Veerji"], [], []),
        ],
    ),
    (
        "Parallel / supportive",
        "",
        [
            ("Quality control", "", ["Tejinderjit Veerji"], [], ["Team"]),
            ("IT", "Software", ["Jashan"], [], ["Team"]),
            ("IT", "Hardware", ["Sumit"], [], ["Team"]),
            ("Accounts & HR", "", ["Shunty Veerji"], ["Kamal"], []),
            ("In-Out", "", ["Jasmeet"], [], []),
        ],
    ),
    (
        "Planning",
        "",
        [
            ("Planning", "", ["Preshit"], [], ["Team"]),
        ],
    ),
]
