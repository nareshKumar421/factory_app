"""
hr_board/constants.py

The HR board's tunables, and the one piece of domain knowledge that decides
whether its headline figure is right or exactly double.

THE LABOUR GATE RECORDS EVERY LABOURER TWICE, ON PURPOSE
--------------------------------------------------------
``labour_gate.LabourGateEntry`` holds two different kinds of row, and they are
told apart by ``department`` alone:

* ``department IS NULL`` -- the **gate intake**. The security post counts heads
  as a contractor's people come through and books them against the contractor,
  the date and the shift. Nobody yet knows where they will work.
* ``department IS NOT NULL`` -- the **allocation**. The HOD then assigns those
  same people to departments, which creates a second set of rows, and the
  intake row is marked fully out (a ``LabourGateOutBatch`` equal to its own
  ``count_in``) to move the headcount across.

So the *same labourer* appears in two rows on a normal day, and
``Sum(count_in)`` over the table is **double the number of people who walked
in**. On the live database the two halves agree on 96% of rows -- they differ
only on the current day, while allocation is still in flight, and on a handful
of days where an HOD never finished.

:data:`INTAKE_IS_DEPARTMENTLESS` is therefore not a filter this module happens
to apply; it is the definition of "how many labourers entered today". It is
recorded here rather than inline in a query because getting it wrong is silent:
the board would simply read high, plausibly, forever.

WHY THERE IS NO "CURRENTLY INSIDE" TILE
---------------------------------------
Because the out-batch mechanism above is an allocation *transfer*, not an exit
record. On the live database all 411 out-batches sit on intake rows and the
1,036 allocated rows have **none** -- allocated labour is never marked out at
all. "Inside now" computed from what remains would count every labourer who
ever entered and never leave, so it is not offered. Build it the day the gate
starts recording real exits against the allocated rows, and not before: a wall
board that invents a number is worse than one that does not show it.
"""

#: What tells an intake row from an allocation row. See the module docstring --
#: this is the whole basis of the daily figure.
INTAKE_IS_DEPARTMENTLESS = True

#: Poll interval the API asks the front end for, in seconds.
#:
#: Slower than the operational boards deliberately. The directory changes when
#: HR types something, and the gate books labour in two bursts a day; a minute's
#: cadence would be twelve hundred reads a day to watch a number that moves
#: twice.
HR_BOARD_REFRESH_SECONDS = 300

#: How many days of labour intake the trend carries.
#:
#: A month, so the weekly shape is visible -- Sundays run about a third of a
#: weekday and a reader who cannot see four of them in a row will read the dip
#: as a problem.
HR_BOARD_TREND_DAYS = 30

#: Departments listed by name on the headcount tile before the rest are summed
#: into one "other" row. The directory has 42 departments with people in them
#: and a wall screen cannot carry 42 bars.
HR_BOARD_TOP_DEPARTMENTS = 8

#: Contractors and departments listed on the labour tile. Six contractors supply
#: the plant today, so this only ever bites if that grows.
HR_BOARD_TOP_LABOUR_ROWS = 10

#: What an empty ``sap_segment`` or a null department is called on screen.
#:
#: Named rather than blank because a bar with no label reads as a rendering
#: fault, and these are real people whose record is simply incomplete.
UNASSIGNED_LABEL = "Unassigned"
