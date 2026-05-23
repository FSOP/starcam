"""Sky event calculator for StarCam.
Solar position & twilight times — pure Python, no external deps.
"""
import math
from datetime import datetime, timedelta, timezone


def _sun_elev(lat_deg: float, lon_deg: float, dt: datetime) -> float:
    """Sun elevation angle (degrees) for a naive UTC datetime."""
    y, mo, d = dt.year, dt.month, dt.day
    h = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    a = (14 - mo) // 12
    Y = y + 4800 - a
    M = mo + 12 * a - 3
    JD = (d + (153 * M + 2) // 5 + 365 * Y + Y // 4
          - Y // 100 + Y // 400 - 32045 + (h - 12) / 24.0)
    T = (JD - 2451545.0) / 36525.0

    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
    M_ = (357.52911 + T * (35999.05029 - 0.0001537 * T)) % 360
    Mr = math.radians(M_)
    C = (math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * T)
         + math.sin(3 * Mr) * 0.000289)

    omega = 125.04 - 1934.136 * T
    lam = math.radians(L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega)))
    eps = math.radians(23.439 - 0.0000004 * (JD - 2451545))

    sin_dec = math.sin(eps) * math.sin(lam)
    dec = math.asin(sin_dec)
    RA = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))

    GMST = (280.46061837 + 360.98564736629 * (JD - 2451545)
            + 0.000387933 * T ** 2 - T ** 3 / 38710000) % 360
    HA = math.radians(GMST + lon_deg) - RA

    lr = math.radians(lat_deg)
    sin_alt = math.sin(lr) * sin_dec + math.cos(lr) * math.cos(dec) * math.cos(HA)
    alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

    # 대기굴절 보정
    if alt > -5.0:
        denom = max(0.01, alt + 10.3 / (alt + 5.11) if alt > -4.7 else 5.0)
        try:
            alt += 1.02 / (60.0 * math.tan(math.radians(denom)))
        except (ValueError, ZeroDivisionError):
            pass
    return alt


def _bisect(lat, lon, t0, t1, target, n=52):
    """Find UTC datetime when sun elevation crosses target between t0..t1."""
    e0 = _sun_elev(lat, lon, t0)
    e1 = _sun_elev(lat, lon, t1)
    if (e0 - target) * (e1 - target) > 0:
        return None
    for _ in range(n):
        tm = t0 + (t1 - t0) / 2
        em = _sun_elev(lat, lon, tm)
        if (e0 - target) * (em - target) <= 0:
            t1 = tm
        else:
            t0, e0 = tm, em
    return t0 + (t1 - t0) / 2


def calc_sky_events(lat: float, lon: float,
                    date_local=None, utc_offset_h: int = 9) -> dict:
    """
    Calculate solar/twilight events for a location and local date.
    Returns HH:MM strings in local time.

    Returned keys:
      sunrise_start/end, civil_start/end, nautical_start/end, astronomical_start/end
      leo_windows: [{period, start, end}]
    """
    tz = timezone(timedelta(hours=utc_offset_h))
    if date_local is None:
        date_local = datetime.now(tz).date()

    # 로컬 정오 → UTC naive
    noon_loc = datetime(date_local.year, date_local.month, date_local.day, 12)
    noon_utc = noon_loc - timedelta(hours=utc_offset_h)
    d_start = noon_utc - timedelta(hours=12)
    d_end   = noon_utc + timedelta(hours=12)

    def _fmt(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%H:%M')

    result = {'date': str(date_local)}

    for name, angle in [('sunrise', -0.833),
                         ('civil',   -6.0),
                         ('nautical', -12.0),
                         ('astronomical', -18.0)]:
        result[f'{name}_end']   = _fmt(_bisect(lat, lon, noon_utc, d_end,   angle))
        result[f'{name}_start'] = _fmt(_bisect(lat, lon, d_start,  noon_utc, angle))

    # LEO 500km 가시 시간대: 시민박명~천문박명
    leo = []
    if result.get('civil_end') and result.get('astronomical_end'):
        leo.append({'period': '저녁', 'start': result['civil_end'],
                    'end': result['astronomical_end']})
    if result.get('astronomical_start') and result.get('civil_start'):
        leo.append({'period': '새벽', 'start': result['astronomical_start'],
                    'end': result['civil_start']})
    result['leo_windows'] = leo
    return result
