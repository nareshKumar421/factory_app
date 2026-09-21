"""
board_builder/cards/

Every card in the palette, one subject per module.

HOW TO ADD ONE
--------------
Put it in the module its subject belongs to (or start a new one and name it in
:func:`load`), write a build function taking a
:class:`~board_builder.catalogue.CardContext` and returning a shape from
``board_builder.viz``, and register it. There is no frontend half.

WHY THE IMPORTS ARE IN A FUNCTION
----------------------------------
A card module imports operational models -- gate, dispatch, production -- and
this package is imported from a catalogue that the URL conf reaches during
startup. Importing models at module scope from there is how a Django project
acquires a circular import that only shows up under one deployment's app
ordering. :func:`load` is called on first use instead, by which point the app
registry is populated.
"""

from __future__ import annotations


def load() -> None:
    """Import every card module once. Idempotent by Python's module cache."""
    from . import chrome, dispatch, gate, production  # noqa: F401
