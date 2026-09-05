"""
The chart as it stands today — the seed for a fresh install.

This is a starting point, not a source of truth: once seeded, the chart is
edited on the page (``org_chart.can_manage_org_chart``) and this list is never
consulted again. ``seed_org_chart`` refuses to overwrite a chart that already
has rows unless it is asked to.
"""

#: (department, [(function, owners, level-01, level-02), ...])
DEFAULT_CHART = [
    (
        "Purchasing",
        [
            ("Oil", ["Shunty Veerji"], ["Raspreet", "Lovepreet", "Gopi"], ["Team"]),
            ("Packaging Material", ["Gagan Veerji"], ["Ravinder Veerji"], ["Team"]),
        ],
    ),
    (
        "Storage",
        [
            ("Oil / Tanks", ["Vicky Veerji"], ["Suni"], ["Team"]),
            ("Packaging Material", ["Kulbeer Veerji"], ["Shahrukh"], ["Team"]),
        ],
    ),
    (
        "Production",
        [
            ("Core Production", ["Vicky Veerji"], ["Gautam"], ["Team"]),
            ("Material Shifting", ["Charanjit Veerji"], ["Monu"], ["Team"]),
            (
                "Warehousing – Main Factory",
                ["Sandeep Veerji"],
                ["Honey", "Tajinder"],
                ["Team"],
            ),
            ("Dispatch – Documentation", ["Sandeep Veerji"], ["Shivam"], ["Raj", "Priya"]),
            ("Dispatch – Docking", ["Sandeep Veerji"], ["Virender Veerji"], ["Team"]),
            ("Transportation", ["Tiwariji"], ["Team"], []),
            ("In & Out", ["Jasmeet"], [], []),
        ],
    ),
    ("Quality Control", [("", ["Tejinderjit Veerji"], ["Team"], [])]),
    (
        "Gupta Godown",
        [
            ("Audit", ["Sandeep Veerji"], ["Honey", "Tajinder"], ["Team"]),
            ("Operations", ["Prince", "Gagan"], ["Arsh", "Santosh", "Jassi"], ["Team"]),
        ],
    ),
    (
        "IT",
        [
            ("Software", ["Jashan"], ["Team"], []),
            ("Hardware", ["Sumit"], ["Team"], []),
        ],
    ),
    ("Accounts & HR", [("", ["Shunty Veerji"], ["Team"], [])]),
]
