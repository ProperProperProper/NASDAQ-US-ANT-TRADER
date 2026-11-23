"""Moomoo 1-minute market-data ingestion for the native research app."""
from __future__ import annotations

import pandas as pd
from moomoo import AuType, KLType, KL_FIELD, OpenQuoteContext, RET_OK

from quant_storage import save_candles


def fetch_moomoo_candles(symbol: str, start: str, end: str) -> int:
    """Fetch and cache only 1-minute candles from the local OpenD service."""
    quote = OpenQuoteContext(host="127.0.0.1", port=11111)
    frames = []
    page_key = None
    try:
        while True:
            ret, data, page_key = quote.request_history_kline(
                code=symbol, start=start, end=end, ktype=KLType.K_1M,
                autype=AuType.QFQ, fields=[KL_FIELD.ALL], max_count=1000,
                page_req_key=page_key, extended_time=False,
            )
            if ret != RET_OK:
                raise RuntimeError(f"Moomoo candle request failed: {data}")
            if data is not None and not data.empty:
                frames.append(data.copy())
            if page_key is None:
                break
    finally:
        quote.close()
    if not frames:
        raise RuntimeError("Moomoo returned no candles for the requested range")
    frame = pd.concat(frames, ignore_index=True)
    frame["time_key"] = pd.to_datetime(frame["time_key"])
    frame = frame.sort_values("time_key").drop_duplicates("time_key").set_index("time_key")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return save_candles(frame, symbol, 1)
