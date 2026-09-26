"""Daily Electricity++: the register's meter tree and who pays for what it measures.

It reads the same meters and readings as the Daily Electricity page, and
nothing else reads it yet: that page and the cost boards keep their own logic.

Three layers, kept apart so the arithmetic can be tested without a database:

* :mod:`.engine`    — the allocation itself. Pure: dataclasses in, dataclasses
  out, no ORM.
* :mod:`.sources`   — reads meters, setups, readings and production run hours
  out of the database into the engine's inputs.
* :mod:`.service`   — what the Daily Electricity++ API calls: "who used how
  many units, and what they cost, between these two dates".
"""
