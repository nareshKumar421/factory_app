"""
Read-only client for the punching machines' SQL Server database.

The machines write into a separate SQL Server box (``Biometrics`` on the factory
LAN), not into this application's Postgres. This module is the only thing that
talks to it, and it only ever reads -- the same boundary
:mod:`sap_client.hana` draws around HANA, and for the same reason: a foreign
system of record with its own schema, its own uptime and its own owner should
be reachable through exactly one door.

**Why a client module and not a second Django database.** A Django connection
alias would want ``managed = False`` models, a database router, and migrations
that carefully avoid the alias. All of that machinery would buy the ability to
write, which is precisely what must never happen here. A function that returns
plain dataclasses is smaller and cannot be misused.

Four things about this schema are not obvious and cost real accuracy if missed.

**``paycode`` is the JWPL employee code.** Not a machine id, not a foreign key:
the same ``JWPL0593`` that HR types into the hierarchy sheet. That is the whole
reason the directory had to be re-imported with JWPL codes before any of this
could work.

**There is no IN/OUT column.** A punch is a timestamp and nothing else, so
direction is *derived*: the first punch of a day is the arrival and the last is
the departure. About 14% of person-days carry a single punch -- somebody who
forgot to punch out -- which is neither present nor absent and is exactly the
case the manual override exists to correct.

**``status`` means "transferred", not "attended".** It holds ``New`` or
``Done``, the flag the vendor's own ETL uses to mark a row as pushed onward.
Reading it as an attendance status would be wrong in a way that looks plausible,
so this module drops the column rather than exposing it.

**Only one of the four punch tables is live.** ``punchtransfer`` runs to today;
``punchtransfer_factory`` stopped in July 2025, ``punchtransfer_factory_sushil``
a month before that, and ``punchtransfer_dsr`` holds field staff on fixed
11:00/17:30 timings rather than real punches. Pointing at an archive table is
silent -- everybody simply reads as absent -- which is why the table name is a
setting with a deliberate default and not a literal scattered through the code.

``ipaddress`` is a device serial (``NCD8244900570``), not an IP, whatever the
column is called; four devices are currently in service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.conf import settings


class BiometricsUnavailable(RuntimeError):
    """The punch database could not be reached or is not configured.

    Raised rather than returning nothing, because "no punches" and "could not
    ask" must never look alike: the first means the factory was closed, the
    second means the dashboard would mark three hundred people absent.
    """


@dataclass(frozen=True)
class Punch:
    """One read of one finger, as the machine recorded it."""

    employee_code: str
    punched_at: datetime
    device: str


def is_configured():
    return bool(getattr(settings, "ATTENDANCE_DB", {}).get("HOST"))


def _connect():
    config = getattr(settings, "ATTENDANCE_DB", {})
    if not config.get("HOST"):
        raise BiometricsUnavailable(
            "ATTENDANCE_DB_HOST is not set -- the punch database is not configured."
        )
    try:
        import pymssql
    except ImportError as exc:  # pragma: no cover - deployment problem, not logic
        raise BiometricsUnavailable(
            "pymssql is not installed; the punch database cannot be read."
        ) from exc

    try:
        return pymssql.connect(
            server=config["HOST"],
            port=str(config.get("PORT") or 1433),
            user=config["USER"],
            password=config["PASSWORD"],
            database=config["NAME"],
            login_timeout=config.get("LOGIN_TIMEOUT", 15),
            timeout=config.get("TIMEOUT", 60),
        )
    except Exception as exc:  # pymssql raises its own hierarchy
        raise BiometricsUnavailable(f"Could not reach the punch database: {exc}") from exc


def _table():
    """The live punch table. See the module docstring on why this is a setting."""
    name = getattr(settings, "ATTENDANCE_PUNCH_TABLE", "punchtransfer")
    # Interpolated into SQL below, so it must be an identifier and nothing else.
    if not name.replace("_", "").isalnum():
        raise BiometricsUnavailable(f"ATTENDANCE_PUNCH_TABLE {name!r} is not a table name.")
    return name


def alias_map(connection=None):
    """``{alias code: real JWPL code}`` for people enrolled under a second code.

    A handful of workers were enrolled on a machine under a ``fac####`` code
    instead of their JWPL one, and ``factory_codes`` is the vendor's mapping
    between the two. It matters more than its size suggests: three people punch
    *only* under the alias, so without this they punch every day and read as
    absent every day.

    Returns an empty map rather than failing if the table is missing -- the
    alias table is a wart, not a dependency.
    """
    own = connection is None
    connection = connection or _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, empcode FROM factory_codes "
                "WHERE id IS NOT NULL AND empcode IS NOT NULL"
            )
            rows = cursor.fetchall()
    except Exception:
        return {}
    finally:
        if own:
            connection.close()

    mapping = {}
    for real, alias in rows:
        real, alias = (real or "").strip().upper(), (alias or "").strip().upper()
        # Only map an alias that differs; a row pointing at itself is noise.
        if real and alias and real != alias:
            mapping[alias] = real
    return mapping


def fetch_punches(date_from: date, date_to: date, *, codes=None):
    """Every punch between two dates inclusive, aliases already resolved.

    ``codes`` restricts the pull to a set of employee codes. It is applied in
    Python rather than SQL: the alias has to be resolved *before* the filter,
    or the three alias-only people are excluded by the very filter meant to
    include them.
    """
    if date_from > date_to:
        raise ValueError("date_from is after date_to")

    connection = _connect()
    try:
        aliases = alias_map(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT paycode, CombinedDatetime, ipaddress
                FROM {_table()}
                WHERE CombinedDatetime >= %s AND CombinedDatetime < DATEADD(day, 1, %s)
                  AND paycode IS NOT NULL AND CombinedDatetime IS NOT NULL
                ORDER BY paycode, CombinedDatetime
                """,
                (date_from, date_to),
            )
            rows = cursor.fetchall()
    except BiometricsUnavailable:
        raise
    except Exception as exc:
        raise BiometricsUnavailable(f"Reading punches failed: {exc}") from exc
    finally:
        connection.close()

    wanted = {code.upper() for code in codes} if codes is not None else None
    punches = []
    for paycode, punched_at, device in rows:
        code = (paycode or "").strip().upper()
        code = aliases.get(code, code)
        if not code:
            continue
        if wanted is not None and code not in wanted:
            continue
        punches.append(Punch(code, punched_at, (device or "").strip()))
    return punches


def health():
    """A one-line description of the punch database, for the status endpoint."""
    try:
        connection = _connect()
    except BiometricsUnavailable as exc:
        return {"reachable": False, "detail": str(exc)}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*), MAX(CombinedDatetime) FROM {_table()}"
            )
            count, latest = cursor.fetchone()
        return {
            "reachable": True,
            "table": _table(),
            "punches": int(count or 0),
            "latest_punch": latest,
        }
    except Exception as exc:
        return {"reachable": False, "detail": str(exc)}
    finally:
        connection.close()
