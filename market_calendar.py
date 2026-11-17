"""Offline exchange sessions; no provider request is needed to render freshness."""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo
import pandas as pd
import pandas_market_calendars as calendars


@lru_cache(maxsize=16)
def schedule(year):
    return calendars.get_calendar('NYSE').schedule(
        start_date=f'{year-1}-12-01', end_date=f'{year+1}-01-15')


def last_close(now=None):
    now = pd.Timestamp(now or datetime.now(ZoneInfo('America/New_York')))
    if now.tzinfo is None:
        raise ValueError('Market time must include a timezone')
    sessions = schedule(now.year)
    eligible = sessions[sessions.market_close <= now]
    return eligible.index[-1].date()


def is_open(now=None):
    now = pd.Timestamp(now or datetime.now(ZoneInfo('America/New_York')))
    if now.tzinfo is None:
        raise ValueError('Market time must include a timezone')
    sessions = schedule(now.year)
    return bool(((sessions.market_open <= now) & (now < sessions.market_close)).any())
