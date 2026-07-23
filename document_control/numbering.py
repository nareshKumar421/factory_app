"""
Pure (no-database) format logic for controlled-document codes.

Format::

    {SECTION}-{DOCTYPE}-{CC}-{SS}-{GG}[-{NN}]

This module knows how to *parse*, *validate* and *format* codes. Sequential
allocation and uniqueness (which need the database) live in
``document_control.services``.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .config import DOCTYPES, SECTIONS

# A CC / SS / GG / NN block is always exactly two digits.
_BLOCK = r"\d{2}"

# {SECTION}-{DOCTYPE}-{CC}-{SS}-{GG}[-{NN}]
CODE_REGEX = re.compile(
    r"^(?P<section>[A-Z]+)"
    r"-(?P<doctype>[A-Z]+)"
    rf"-(?P<cc>{_BLOCK})"
    rf"-(?P<ss>{_BLOCK})"
    rf"-(?P<gg>{_BLOCK})"
    rf"(?:-(?P<nn>{_BLOCK}))?$"
)

# A clause is the three two-digit blocks "CC-SS-GG".
CLAUSE_REGEX = re.compile(rf"^(?P<cc>{_BLOCK})-(?P<ss>{_BLOCK})-(?P<gg>{_BLOCK})$")


class InvalidDocumentCode(ValueError):
    """Raised when a string is not a well-formed / known document code."""


@dataclass(frozen=True)
class ParsedCode:
    """The parts of a document code."""

    section: str
    doctype: str
    cc: str
    ss: str
    gg: str
    nn: Optional[str] = None  # None when the optional serial is absent.

    @property
    def clause(self) -> str:
        """The CC-SS-GG clause triple."""
        return f"{self.cc}-{self.ss}-{self.gg}"

    @property
    def serial(self) -> Optional[int]:
        """The NN serial as an int, or ``None`` when absent."""
        return int(self.nn) if self.nn is not None else None

    @property
    def group_key(self) -> tuple:
        """Identity of the numbering group this code belongs to."""
        return (self.section, self.doctype, self.cc, self.ss, self.gg)

    @property
    def section_name(self) -> str:
        return SECTIONS.get(self.section, "")

    @property
    def doctype_name(self) -> str:
        return DOCTYPES.get(self.doctype, "")

    def __str__(self) -> str:
        return format_code(self.section, self.doctype, self.clause, self.nn)


def split_clause(clause: str) -> tuple:
    """Return ``(cc, ss, gg)`` for a ``"CC-SS-GG"`` clause string.

    Raises :class:`InvalidDocumentCode` if the clause is malformed.
    """
    match = CLAUSE_REGEX.match((clause or "").strip())
    if not match:
        raise InvalidDocumentCode(
            f"Clause {clause!r} must be three two-digit blocks 'CC-SS-GG' "
            f"(e.g. '04-02-00')."
        )
    return match.group("cc"), match.group("ss"), match.group("gg")


def validate_parts(section: str, doctype: str, clause: str) -> None:
    """Validate the section / doctype / clause used to *mint* a new code.

    Raises :class:`InvalidDocumentCode` on any unknown or malformed part.
    """
    if section not in SECTIONS:
        raise InvalidDocumentCode(
            f"Unknown SECTION {section!r}. Known sections: "
            f"{', '.join(sorted(SECTIONS))}."
        )
    if doctype not in DOCTYPES:
        raise InvalidDocumentCode(
            f"Unknown DOCTYPE {doctype!r}. Known document types: "
            f"{', '.join(sorted(DOCTYPES))}."
        )
    split_clause(clause)  # raises on a bad clause


def format_code(section: str, doctype: str, clause: str, nn=None) -> str:
    """Build a code string from its parts.

    ``nn`` may be an int, a string, or ``None`` (serial omitted). It is always
    zero-padded to two digits when present.
    """
    cc, ss, gg = split_clause(clause)
    base = f"{section}-{doctype}-{cc}-{ss}-{gg}"
    if nn is None or nn == "":
        return base
    return f"{base}-{int(nn):02d}"


def parse_code(code: str) -> ParsedCode:
    """Parse a code into its parts.

    Validates both the *format* and that the SECTION / DOCTYPE are known.
    Raises :class:`InvalidDocumentCode` otherwise.
    """
    match = CODE_REGEX.match((code or "").strip())
    if not match:
        raise InvalidDocumentCode(
            f"{code!r} is not a valid document code. Expected "
            f"'{{SECTION}}-{{DOCTYPE}}-{{CC}}-{{SS}}-{{GG}}[-{{NN}}]'."
        )
    section = match.group("section")
    doctype = match.group("doctype")
    if section not in SECTIONS:
        raise InvalidDocumentCode(f"Unknown SECTION {section!r} in code {code!r}.")
    if doctype not in DOCTYPES:
        raise InvalidDocumentCode(f"Unknown DOCTYPE {doctype!r} in code {code!r}.")
    return ParsedCode(
        section=section,
        doctype=doctype,
        cc=match.group("cc"),
        ss=match.group("ss"),
        gg=match.group("gg"),
        nn=match.group("nn"),
    )


def is_valid_code(code: str) -> bool:
    """Return ``True`` if ``code`` is a well-formed, known document code."""
    try:
        parse_code(code)
        return True
    except InvalidDocumentCode:
        return False
