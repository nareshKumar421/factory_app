# Employee data collection

`Employee_Data_Collection_Template.xlsx` is the workbook we hand to a company to
collect everything the employee-hierarchy module needs: departments,
designations, people, salary history and who is allowed to see what.

`build_collection_template.py` writes it:

```bash
python employee_hierarchy/docs/build_collection_template.py
```

Neither file is part of the application — nothing imports them and nothing at
runtime depends on them. The script exists so the dropdown lists can be
regenerated from `employee_hierarchy.constants` the day one of those lists
changes. A template whose "Employment status" list has drifted from the code is
worse than no template at all, because the data comes back looking valid and
imports wrong.

## What the workbook contains

| Sheet | What goes in it |
|---|---|
| **Start here** | Instructions, the colour key, and a short form asking which company unit the file covers and who prepared it. |
| **1. Departments** | The department tree — each row's parent is a code from the same sheet. |
| **2. Designations** | The ladder, each rung with its organisational level. |
| **3. Employees** | Everybody, with department, designation and reporting manager written as **codes**. |
| **4. Salary history** | **One row per salary**, not per person. Three raises means three rows. |
| **5. Access and roles** | Who logs in, which employee they are, and the smallest role that lets them work. |
| **Example (filled in)** | A seven-person company filled in properly, so the codes can be seen joining up. |
| **Allowed values** | Every value the dropdowns accept, and what each one means. Named ranges live here. |
| **Checks** | Live formulas counting everything still wrong. All the "must" rows have to read 0. |

## The three things the design is trying to prevent

**Names where codes belong.** Every cross-sheet reference is a short code chosen
from a dropdown, because free-typed names arrive as four spellings of one
department and each spelling becomes a separate row on import. Beside every code
column sits a grey `(auto)` column that looks the name up, so a wrong code is
visible while the person who knows the answer is still looking at the row.

**Monthly figures in annual columns.** Every money column on sheet 4 is per
year, says so in its heading, and the calculated total is there to be read back
against the offer letter.

**Everybody made an administrator.** Sheet 5's role list explains what each role
can see before it asks anyone to choose, and says to pick the smaller one when
in doubt. Salary is the part of this system that cannot be un-seen.

## Loading it

There is no importer in the app — this is a hand-over format, not a feature.
The data is loaded by whoever runs the migration, in sheet order (departments,
designations, employees, then salary), because each sheet references the ones
before it. Reporting loops deeper than two people are the one error the workbook
cannot catch; they surface on import and go back to the sender with names
attached.
