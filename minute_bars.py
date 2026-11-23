"""Build US regular-session bars from a single 1-minute candle feed."""
from __future__ import annotations

import pandas as pd


MINUTE_INTERVALS = tuple(range(1, 22))


def aggregate_us_session(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate complete 09:30–16:00 US session buckets from 1m candles."""
    if minutes not in MINUTE_INTERVALS:
        raise ValueError("minutes must be between 1 and 21")
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    x = frame.copy()
    x.index = pd.to_datetime(x.index)
    x = x.sort_index().between_time("09:30", "16:00", inclusive="left")
    rows = []
    for _, group in x.groupby(x.index.date):
        grouped = group.groupby(pd.Grouper(freq=f"{minutes}min", origin="start_day", offset="9h30min"))
        for stamp, part in grouped:
            if len(part) != minutes:
                continue
            rows.append({
                "time_key": stamp,
                "open": float(part.open.iloc[0]),
                "high": float(part.high.max()),
                "low": float(part.low.min()),
                "close": float(part.close.iloc[-1]),
                "volume": float(part.volume.fillna(0).sum()) if "volume" in part else 0.0,
            })
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.DataFrame(rows).drop_duplicates("time_key").set_index("time_key").sort_index()
