"""
The search box, parsed.

The issue list's one text input speaks GitHub's qualifier language, because that
is what the box looks like and anything else would surprise the people who type
into it::

    is:open label:bug assignee:@me sort:created-desc
    is:closed no:assignee "gate pass"
    author:jashan@alise.in priority:urgent scan

Anything that is not a ``key:value`` pair is free text and is matched against
the title, the body and the issue number. Quotes keep a phrase together.

This module is deliberately pure -- it turns a string into a
:class:`ParsedQuery` and does not touch the database. :func:`issues.services.
search_issues` is what applies it to a queryset, so the parsing is testable on
its own and the two concerns stay apart.

Unknown qualifiers are NOT silently dropped into free text: a typo like
``labels:bug`` would then quietly match nothing while looking like it worked.
They are collected in ``unknown`` and the API reports them back so the client
can say so.
"""

import re
from dataclasses import dataclass, field

from .constants import IssuePriority, IssueState, StateReason

#: ``key:value`` where the value may be quoted to hold spaces.
_QUALIFIER = re.compile(
    r'(?P<negate>-?)(?P<key>[a-zA-Z_]+):(?P<value>"[^"]*"|\'[^\']*\'|[^\s]+)'
)

#: A bare quoted phrase, e.g. "gate pass".
_PHRASE = re.compile(r'"([^"]*)"|\'([^\']*)\'')

#: The qualifiers the box understands. Anything else is reported as unknown.
KNOWN_KEYS = frozenset(
    {
        "is",
        "state",
        "label",
        "assignee",
        "author",
        "priority",
        "company",
        "reason",
        "no",
        "sort",
        "number",
        "involves",
        "commenter",
    }
)

#: ``sort:`` values, mapped to the ordering key :func:`issues.services` applies.
SORT_ALIASES = {
    "created": "-created",
    "created-desc": "-created",
    "created-asc": "created",
    "newest": "-created",
    "oldest": "created",
    "updated": "-updated",
    "updated-desc": "-updated",
    "updated-asc": "updated",
    "comments": "-comments",
    "comments-desc": "-comments",
    "comments-asc": "comments",
    "priority": "priority",
    "priority-desc": "priority",
}

#: ``no:`` values -- "has nothing in this field". Priority is deliberately absent:
#: it always holds a value (Medium by default), so ``no:priority`` could never
#: match and is better reported as a mistake than accepted and ignored.
EMPTY_FIELDS = frozenset({"assignee", "label"})


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


@dataclass
class ParsedQuery:
    """What the search box asked for.

    Every list field is an OR within itself and an AND across fields, matching
    GitHub: ``label:bug label:ui`` means "labelled bug **and** ui" for labels
    (GitHub ANDs repeated labels), while ``assignee:a assignee:b`` means either
    of them. Those two really do differ there, and the difference is the useful
    one, so it is kept.
    """

    text: str = ""
    state: str = ""
    labels: list = field(default_factory=list)
    exclude_labels: list = field(default_factory=list)
    assignees: list = field(default_factory=list)
    authors: list = field(default_factory=list)
    priorities: list = field(default_factory=list)
    companies: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    involves: list = field(default_factory=list)
    commenters: list = field(default_factory=list)
    numbers: list = field(default_factory=list)
    empty: list = field(default_factory=list)
    sort: str = ""
    unknown: list = field(default_factory=list)

    @property
    def is_empty(self):
        """True when the query asks for nothing at all."""
        return self == ParsedQuery()


def parse_query(raw):
    """Parse the search box into a :class:`ParsedQuery`. Never raises."""
    query = ParsedQuery()
    if not raw:
        return query

    remainder = raw
    for match in _QUALIFIER.finditer(raw):
        key = match.group("key").lower()
        value = _unquote(match.group("value")).strip()
        negated = match.group("negate") == "-"
        # Consume the qualifier so it does not also become free text -- including
        # an unknown one, which would otherwise be searched for literally and
        # quietly return nothing while looking like a working filter.
        remainder = remainder.replace(match.group(0), " ", 1)
        if key not in KNOWN_KEYS:
            query.unknown.append(match.group(0))
            continue
        if not value:
            continue
        _apply(query, key, value, negated)

    # Whatever is left over is the free-text search; quotes are just grouping.
    text_parts = []
    for phrase_match in _PHRASE.finditer(remainder):
        phrase = phrase_match.group(1) or phrase_match.group(2) or ""
        if phrase.strip():
            text_parts.append(phrase.strip())
        remainder = remainder.replace(phrase_match.group(0), " ", 1)
    text_parts.extend(remainder.split())
    query.text = " ".join(text_parts).strip()

    # "#41" or a bare number in the free text is a jump to that issue number,
    # not a body search -- it is how everyone refers to an issue.
    if query.text:
        bare = query.text.lstrip("#")
        if bare.isdigit():
            query.numbers.append(int(bare))
            query.text = ""

    return query


def _apply(query, key, value, negated):
    lowered = value.lower()

    if key in {"is", "state"}:
        if lowered in {"open", "closed"}:
            query.state = (
                IssueState.OPEN if lowered == "open" else IssueState.CLOSED
            )
        elif lowered in {"pinned", "locked", "unlocked", "unpinned"}:
            query.empty.append(f"flag:{lowered}")
        else:
            query.unknown.append(f"{key}:{value}")
        return

    if key == "label":
        (query.exclude_labels if negated else query.labels).append(value)
        return

    if key == "assignee":
        query.assignees.append(value)
        return

    if key == "author":
        query.authors.append(value)
        return

    if key == "priority":
        upper = value.upper()
        if upper in IssuePriority.values:
            query.priorities.append(upper)
        else:
            query.unknown.append(f"{key}:{value}")
        return

    if key == "company":
        query.companies.append(value)
        return

    if key == "reason":
        upper = value.upper().replace("-", "_")
        if upper in StateReason.values:
            query.reasons.append(upper)
        else:
            query.unknown.append(f"{key}:{value}")
        return

    if key == "involves":
        query.involves.append(value)
        return

    if key == "commenter":
        query.commenters.append(value)
        return

    if key == "number":
        stripped = value.lstrip("#")
        if stripped.isdigit():
            query.numbers.append(int(stripped))
        else:
            query.unknown.append(f"{key}:{value}")
        return

    if key == "no":
        if lowered in EMPTY_FIELDS:
            query.empty.append(lowered)
        else:
            query.unknown.append(f"{key}:{value}")
        return

    if key == "sort":
        resolved = SORT_ALIASES.get(lowered)
        if resolved:
            query.sort = resolved
        else:
            query.unknown.append(f"{key}:{value}")
        return
