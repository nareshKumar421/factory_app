"""JSON that gives back what was put in.

A copied SAP read has to come back as the same types the live read returns --
``Decimal`` quantities, ``date`` stamps -- or a caller doing arithmetic on a
bill served from the copy fails only while HANA is down, the one time nobody
can debug it. Plain JSON would hand back strings, so each such value is stored
tagged and rebuilt on the way out.
"""

from datetime import date, datetime, time
from decimal import Decimal


def pack(value):
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    if isinstance(value, dict):
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [pack(item) for item in value]
    return value


_UNPACK = {
    "$dec": Decimal,
    "$dt": datetime.fromisoformat,
    "$date": date.fromisoformat,
    "$time": time.fromisoformat,
}


def unpack(value):
    if isinstance(value, dict):
        if len(value) == 1:
            (tag, raw), = value.items()
            if tag in _UNPACK:
                return _UNPACK[tag](raw)
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value
