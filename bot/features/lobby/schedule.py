"""Parse the "when" of a scheduled lobby ("21:00", "amanhã 21h30", "sexta 21:00", "15/08 21:00").

Pure and testable: `now` is passed in. Times are read in Europe/Lisbon (where the friends are)
and returned as aware UTC datetimes, so daylight-saving changes are handled by the time zone
database, not by this module.
"""

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LISBON = ZoneInfo("Europe/Lisbon")
EXAMPLES = "21:00, 21h30, amanhã 21:00, sexta 21:00, 15/08 21:00 ou 2026-08-15 21:00"

WEEKDAYS = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
TODAY = ("hoje", "today")
TOMORROW = ("amanha", "tomorrow")

_TIME = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2})|h(\d{2})?)(?!\d)")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?")


class ScheduleError(ValueError):
    """The text could not be understood as a time (the message says how to write it)."""


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _bad(detail: str = "") -> ScheduleError:
    prefix = f"{detail} " if detail else ""
    return ScheduleError(f"{prefix}Escreve a hora assim: {EXAMPLES}.")


def parse_when(text: str, now: datetime, tz: ZoneInfo = LISBON) -> datetime:
    """The moment described by `text`, as an aware UTC datetime strictly after `now`."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    plain = " ".join(_plain(text).replace("-feira", "").split())
    match = _TIME.search(plain)
    if not match:
        raise _bad("Falta a hora.")
    hour = int(match.group(1))
    minute = int(match.group(2) or match.group(3) or 0)
    if hour > 23 or minute > 59:
        raise _bad("Hora inválida.")
    clock = time(hour, minute)
    rest = (plain[: match.start()] + " " + plain[match.end() :]).replace(" as ", " ").strip()
    rest = " ".join(rest.replace(" a ", " ").split())

    local_now = now.astimezone(tz)
    today = local_now.date()
    explicit_day = True
    day: date

    if rest == "":
        day, explicit_day = today, False
    elif rest in TODAY:
        day = today
    elif rest in TOMORROW:
        day = today + timedelta(days=1)
    elif rest in WEEKDAYS:
        ahead = (WEEKDAYS[rest] - today.weekday()) % 7
        day = today + timedelta(days=ahead)
        if ahead == 0 and datetime.combine(day, clock, tz) <= local_now:
            day += timedelta(days=7)
    elif iso := _ISO.fullmatch(rest):
        day = _date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    elif dmy := _DMY.fullmatch(rest):
        year = int(dmy.group(3)) if dmy.group(3) else today.year
        day = _date(year, int(dmy.group(2)), int(dmy.group(1)))
        if not dmy.group(3) and day < today:
            day = _date(year + 1, day.month, day.day)
    else:
        raise _bad("Não percebi o dia.")

    moment = datetime.combine(day, clock, tz)
    if moment <= local_now:
        if explicit_day:
            raise ScheduleError("Essa hora já passou.")
        moment += timedelta(days=1)  # "21:00" when it is already past 21:00 means tomorrow
    return moment.astimezone(UTC)


def _date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise _bad("Data inválida.") from None
