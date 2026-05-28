"""
Stdlib-only Gregorian ↔ Jalali (Shamsi/Persian) calendar conversion.

We don't pull in jdatetime / persiantools because (a) one new dep, (b) we
only need one direction (Gregorian → Jalali for display), and (c) the
algorithm is ~30 lines of arithmetic. This is the Borkowski algorithm,
which is what every Iranian government time conversion ultimately uses.

Public surface:
    gregorian_to_jalali(year, month, day) -> (jy, jm, jd)
    format_jalali(dt) -> "1404/10/07 18:08:08"  (or just date if dt is date)
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta


def _div(a: int, b: int) -> int:
    # Integer division compatible with the algorithm's expectation
    # (Python's // already floors, which is what we want).
    return a // b


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian (gy, gm, gd) date to Jalali (jy, jm, jd)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gm > 2 and (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)):
        days = 79
    else:
        days = 79
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = (
        365 * gy2
        + _div(gy2 + 3, 4)
        - _div(gy2 + 99, 100)
        + _div(gy2 + 399, 400)
    )
    for i in range(gm2):
        g_day_no += g_d_m[i + 1] - g_d_m[i] if i + 1 < len(g_d_m) else 31
    if gm2 > 1 and (
        (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    ):
        g_day_no += 1
    g_day_no += gd2

    j_day_no = g_day_no - 79
    j_np = _div(j_day_no, 12053)
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * _div(j_day_no, 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += _div(j_day_no - 1, 365)
        j_day_no = (j_day_no - 1) % 365

    j_d_m = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    for jm in range(11):
        if j_day_no < j_d_m[jm]:
            break
        j_day_no -= j_d_m[jm]
    else:
        jm = 11
    jd = j_day_no + 1
    return jy, jm + 1, jd


def format_jalali(dt: datetime | date | None, *, with_time: bool = True) -> str:
    """Format a datetime/date as 'YYYY/MM/DD HH:MM:SS' in Jalali calendar.

    Returns "—" for None. Always renders Western digits (we use Latin
    digits everywhere else in the bot for consistency).
    """
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Convert to Iran Standard Time (UTC+3:30) so the operator sees
        # the time they actually experienced.
        local = dt.astimezone(timezone(timedelta(hours=3, minutes=30)))
        jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
        if with_time:
            return f"{jy:04d}/{jm:02d}/{jd:02d} {local.hour:02d}:{local.minute:02d}:{local.second:02d}"
        return f"{jy:04d}/{jm:02d}/{jd:02d}"
    # It's a date
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"
