"""Session clock for crypto options (24×7 + expiry cutoff)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def is_nse_weekday(d: date | None = None) -> bool:
    """Unused for crypto; kept for shared test imports. Always True."""
    del d
    return True


def is_market_open(
    market_session: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Crypto is 24×7 unless twenty_four_seven is false."""
    if bool(market_session.get("twenty_four_seven", True)):
        if bool(market_session.get("skip_weekends", False)):
            ist = now.astimezone(IST) if now is not None else datetime.now(IST)
            if ist.weekday() >= 5:
                return False
        return True
    ist = now.astimezone(IST) if now is not None else datetime.now(IST)
    open_h, open_m = map(int, str(market_session["market_open"]).split(":"))
    close_h, close_m = map(int, str(market_session["market_close"]).split(":"))
    open_ts = ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_ts = ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_ts <= ist <= close_ts


def session_label(
    market_session: dict,
    *,
    now: datetime | None = None,
) -> str:
    ist = now.astimezone(IST) if now is not None else datetime.now(IST)
    if not is_market_open(market_session, now=ist):
        return "CLOSED"
    # 24×7 crypto: stay OPEN after daily expiry roll; entry cutoff is handled separately.
    if bool(market_session.get("twenty_four_seven", True)):
        return "OPEN"
    cutoff = str(market_session.get("expiry_force_exit_time", "17:30"))
    try:
        hh, mm = map(int, cutoff.split(":"))
        if ist.hour > hh or (ist.hour == hh and ist.minute >= mm):
            return "EXPIRY"
    except Exception:
        pass
    return "OPEN"


def past_expiry_entry_cutoff(market_session: dict, *, now: datetime | None = None) -> bool:
    """True when new entries should stop near daily options expiry."""
    ist = now.astimezone(IST) if now is not None else datetime.now(IST)
    cutoff = str(market_session.get("expiry_force_exit_time", "17:30"))
    mins = int(market_session.get("expiry_entry_cutoff_minutes", 45))
    try:
        hh, mm = map(int, cutoff.split(":"))
        exit_t = ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ist >= exit_t - timedelta(minutes=mins)
    except Exception:
        return False
