from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from bot.features.lobby.schedule import LISBON, ScheduleError, parse_when

# Wednesday 2026-07-01 18:00 in Lisbon (WEST, UTC+1) = 17:00 UTC.
NOW = datetime(2026, 7, 1, 17, 0, tzinfo=UTC)


def lisbon(moment: datetime) -> str:
    return moment.astimezone(LISBON).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("21:00", "2026-07-01 21:00"),
        ("21h", "2026-07-01 21:00"),
        ("21h30", "2026-07-01 21:30"),
        ("9:05", "2026-07-02 09:05"),  # already past 9:05 today: means tomorrow
        ("18:00", "2026-07-02 18:00"),  # "now" is not in the future: tomorrow
        ("hoje 22:00", "2026-07-01 22:00"),
        ("Hoje às 22h", "2026-07-01 22:00"),
        ("amanhã 21:00", "2026-07-02 21:00"),
        ("amanha 21h30", "2026-07-02 21:30"),
        ("tomorrow 20:00", "2026-07-02 20:00"),
        ("sexta 21:00", "2026-07-03 21:00"),
        ("sexta-feira 21:00", "2026-07-03 21:00"),
        ("Sábado 15h", "2026-07-04 15:00"),
        ("terça 21:00", "2026-07-07 21:00"),
        ("quarta 21:00", "2026-07-01 21:00"),  # today is Wednesday and 21:00 is still ahead
        ("quarta 12:00", "2026-07-08 12:00"),  # ... but 12:00 is behind us: next week
        ("friday 21:00", "2026-07-03 21:00"),
        ("15/07 21:00", "2026-07-15 21:00"),
        ("15/07/2026 21:00", "2026-07-15 21:00"),
        ("2026-07-10 20:30", "2026-07-10 20:30"),
        ("  AMANHÃ   21:00 ", "2026-07-02 21:00"),
        ("25/06 21:00", "2027-06-25 21:00"),  # a date without year that has passed: next year
    ],
)
def test_accepted_formats(text, expected):
    assert lisbon(parse_when(text, NOW)) == expected


def test_result_is_utc_and_follows_lisbon_daylight_saving():
    summer = parse_when("21:00", NOW)
    assert summer.tzinfo == UTC
    assert summer == datetime(2026, 7, 1, 20, 0, tzinfo=UTC)  # UTC+1 in July
    winter_now = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    winter = parse_when("21:00", winter_now)
    assert winter == datetime(2026, 12, 1, 21, 0, tzinfo=UTC)  # UTC+0 in December


def test_a_different_time_zone_can_be_used():
    moment = parse_when("21:00", NOW, ZoneInfo("America/Sao_Paulo"))
    assert moment == datetime(2026, 7, 2, 0, 0, tzinfo=UTC)  # 21:00 BRT is 00:00 UTC next day


def test_the_result_is_always_in_the_future():
    for text in ("18:00", "17:59", "18h01", "hoje 18:01"):
        assert parse_when(text, NOW) > NOW


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Falta a hora"),
        ("amanhã", "Falta a hora"),
        ("25:00", "Hora inválida"),
        ("21:75", "Hora inválida"),
        ("hoje 10:00", "já passou"),
        ("2026-06-01 21:00", "já passou"),
        ("30/02 21:00", "Data inválida"),
        ("blah 21:00", "Não percebi o dia"),
        ("sexta sábado 21:00", "Não percebi o dia"),
    ],
)
def test_bad_input_explains_how_to_write_it(text, message):
    with pytest.raises(ScheduleError, match=message) as info:
        parse_when(text, NOW)
    if "já passou" not in message:
        assert "21:00" in str(info.value)  # examples are included


def test_now_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_when("21:00", datetime(2026, 7, 1, 17, 0))
