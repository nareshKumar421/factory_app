"""
control_boards/sections.py

Assembling a board out of sections, where a section can fail in two very
different ways.

WITHHELD IS NOT DEGRADED
------------------------
``plant_board`` and ``admin_board`` already report ``meta.degraded`` -- a list of
the sections that could not be READ, because SAP timed out or a query blew up.
Per-feed rights introduce a second reason a section can be missing: the reader is
not allowed to see it.

These must never share a list. A board that reported "you may not read this" as
"the source is down" would have somebody chasing a HANA outage that is not
happening, and a board that reported an outage as a permission problem would
send them to an administrator instead of to the server room. So:

    meta.degraded  -- we tried to read it and could not
    meta.withheld  -- we did not try, because this reader may not see it

The distinction is the same one the cost tile already draws with ``has_source``,
separating "genuinely nil" from "nobody configured a rate". A number and the
absence of a number are different facts, and so are the reasons for an absence.

THE LATCH
---------
``needs_sap`` and the ``_sap_down`` latch are carried over from
``plant_board/services.py`` unchanged, including the reason, which was measured
rather than guessed: a HANA connect attempt blocks for about fifteen seconds, and
six of those in a row is past the client's own timeout, so the whole board came
back as a failed request and the screen went blank. Once one section reports the
connection is gone, the rest stop asking.

Note the ORDER: the permission check runs BEFORE the latch. A withheld section
must read as withheld even during a SAP outage, because whether somebody is
allowed to see a tile has nothing to do with whether SAP is answering.

ALERTS
------
Anything deriving actions from a composed board must skip a withheld section
exactly as it already skips a degraded one. ``admin_board/alerts.py`` puts it
best: a silent all-clear derived from a read that never happened reports a
healthy plant on the strength of having learned nothing about it. "Nobody showed
me the dispatch tile" is not "dispatch is fine".
"""

from __future__ import annotations

import logging
from typing import Callable

from sap_client.exceptions import SAPConnectionError, SAPDataError

from .feeds import may_read

logger = logging.getLogger(__name__)


def is_withheld(user, feed: str | None) -> bool:
    """Whether a section must be hidden from this reader.

    THE ONE PLACE this decision is made. ``admin_board`` and ``plant_board``
    keep their own ``_section`` -- their degraded/latch handling differs in ways
    that are load-bearing and not worth unifying -- but they call this, so the
    three boards cannot drift about who may see what. The security decision is
    shared even where the plumbing is not.

    ``user is None`` withholds nothing. A service built without a request (every
    existing test, and the injectable-collaborator pattern both boards rely on)
    behaves exactly as it did before feed rights existed.
    """
    return feed is not None and user is not None and not may_read(user, feed)


class SectionBuilder:
    """Mixin for a board service: run sections, record why each one is absent.

    Subclasses set ``self.user`` (may be ``None``) and call ``self.section(...)``
    for each band, then put ``self.section_meta()`` into the payload's ``meta``.

    ``user=None`` withholds nothing, which is what keeps every existing test and
    the injectable-collaborator pattern working -- a service built without a
    request behaves exactly as it did before feed rights existed.
    """

    #: Set by subclasses before the first ``section()`` call.
    user = None

    def _init_sections(self) -> None:
        self._degraded: list[str] = []
        self._withheld: list[str] = []
        self._warnings: list[str] = []
        self._sap_down = False

    def section(
        self,
        name: str,
        build: Callable[[], object],
        *,
        needs_sap: bool = True,
        feed: str | None = None,
    ):
        """Build one section, or record why it is missing and return ``None``.

        A missing section is always ``None`` in the payload -- never omitted,
        never ``{}``, never zero. A zero that nobody can explain gets believed
        for a week and then ignored forever.
        """
        # Permission first, and before the SAP latch on purpose: what somebody
        # is allowed to see does not depend on whether SAP is answering.
        if is_withheld(self.user, feed):
            self._withheld.append(name)
            return None

        if needs_sap and self._sap_down:
            self._degraded.append(name)
            return None

        try:
            return build()
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("Board section %r could not be read: %s", name, exc)
            if isinstance(exc, SAPConnectionError):
                # Latch: one outage costs one timeout, not one per section.
                self._sap_down = True
            self._degraded.append(name)
            return None
        except Exception:  # noqa: BLE001 - a board must not 500 over one tile
            logger.exception("Board section %r failed", name)
            self._degraded.append(name)
            return None

    def warn(self, message: str) -> None:
        """Add a prose caveat for a section that DID render.

        Not for absence -- an absent section is named in ``degraded`` or
        ``withheld`` and needs no prose to be understood.
        """
        if message not in self._warnings:
            self._warnings.append(message)

    def section_meta(self) -> dict:
        """The three lists, for splicing into the payload's ``meta``."""
        if self._sap_down:
            self.warn(
                "SAP did not answer. The bands that read it are showing nothing "
                "rather than a stale figure."
            )
        return {
            "degraded": list(self._degraded),
            "withheld": list(self._withheld),
            "warnings": list(self._warnings),
        }
