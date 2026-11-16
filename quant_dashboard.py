#!/usr/bin/env python3
"""Local Moomoo-style tester/optimizer dashboard with paper execution.

The backtest side accepts Moomoo-exported OHLC CSV/Parquet files and reuses the
project's local Quant-style engine. The execution side starts the local bot
with TrdEnv.SIMULATE only; it refuses accounts that are not reported as
SIMULATE by OpenD.

Run:
    python3 quant_dashboard.py
Then open http://127.0.0.1:8780.
"""

from __future__ import annotations

import json
import logging
import math
import os
import plistlib
import subprocess
import threading
import time
import fcntl
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd
from urllib.parse import urlparse

from competition_bot import BAR_INTERVAL_TYPES, DEFAULT_BAR_INTERVAL, CompetitionBot
from BBI_5m_Backtest.local_quant_tester import charts
from strategy_57pct import BASELINE, run as strategy_run, walk_forward_optimize, walk_forward_replay
from strategy_57pct import stats as strategy_stats
from moomoo import AuType, KLType, KL_FIELD, OpenQuoteContext, OpenSecTradeContext, RET_OK, TrdMarket
from quant_storage import load_candles, save_backtest, save_candles
from quant_api import data_api, risk_json, strategy_api


ROOT = Path(__file__).resolve().parent
PORT = 8780
MAX_BACKTEST_DAYS = 30
PAPER_TRADING_ENABLED = True
EXTERNAL_BOT_LABEL = "com.local.nasdaq-paper-bot"
PAPER_BOT_PLIST = Path.home() / "Library/LaunchAgents/com.local.nasdaq-paper-bot.plist"
PAPER_ACCOUNT_ID = COMPETITION_ACCOUNT_ID
INSTANCE_LOCK_PATH = Path(__file__).with_name("quant_dashboard.lock")
SYMBOL = "US.APP"
INTERVAL_MINUTES = 1440
bot_lock = threading.Lock()
bot_instance: CompetitionBot | None = None
bot_thread: threading.Thread | None = None
last_backtest: dict[str, Any] = {}
backtest_lock = threading.Lock()
backtest_job: dict[str, Any] = {"state": "idle", "done": 0, "total": 0, "message": "Idle"}
backtest_cancel = threading.Event()


def optimize(frame, base, capital, allocation, slip, commission, progress=None):
    return walk_forward_optimize(frame, base, capital, allocation, slip, commission, progress)

def normalize_date(value: Any, field: str) -> str:
    try:
        parsed = pd.to_datetime(value, errors="raise")
    except Exception as exc:
        raise ValueError(f"Invalid {field} date: {value!r}; use YYYY-MM-DD") from exc
    if pd.isna(parsed):
        raise ValueError(f"Invalid {field} date: {value!r}; use YYYY-MM-DD")
    return parsed.strftime("%Y-%m-%d")


def accounts() -> list[dict[str, Any]]:
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(str(data))
        return json.loads(data.to_json(orient="records"))
    finally:
        ctx.close()


def fetch_moomoo_candles(symbol: str, start: str, end: str, interval_minutes: int) -> int:
    interval_types = {1: KLType.K_1M, 3: KLType.K_3M, 5: KLType.K_5M, 10: KLType.K_10M, 15: KLType.K_15M, 30: KLType.K_30M, 1440: KLType.K_DAY}
    if interval_minutes not in interval_types:
        raise ValueError("Interval must be 1, 3, 5, 10, 15, 30, or 1440 minutes")
    quote = OpenQuoteContext(host="127.0.0.1", port=11111)
    frames = []
    page_key = None
    try:
        while True:
            ret, data, page_key = quote.request_history_kline(
                code=symbol, start=start, end=end, ktype=interval_types[interval_minutes],
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
    return save_candles(frame, symbol, interval_minutes)


def execution_status() -> dict[str, Any]:
    with bot_lock:
        active = bot_instance is not None and bot_instance.running
        status = {
            "active": active,
            "symbols": list(bot_instance.symbols) if bot_instance else [],
            "account_index": bot_instance.acc_index if bot_instance else None,
            "account_id": bot_instance.acc_id if bot_instance else None,
            "mode": "SIMULATE" if active else "STOPPED",
        }
    if active:
        return status
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{EXTERNAL_BOT_LABEL}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0 and "state = running" in result.stdout:
            pid = next((line.split("=", 1)[1].strip() for line in result.stdout.splitlines() if line.strip().startswith("pid =")), None)
            bar_interval = DEFAULT_BAR_INTERVAL
            try:
                with PAPER_BOT_PLIST.open("rb") as handle:
                    args = plistlib.load(handle).get("ProgramArguments", [])
                if "--bar-interval" in args:
                    bar_interval = int(args[args.index("--bar-interval") + 1])
            except (OSError, ValueError, TypeError, IndexError):
                pass
            return {
                "active": True,
                "symbols": ["US.APP"],
                "account_index": None,
                "account_id": COMPETITION_ACCOUNT_ID,
                "mode": "SIMULATE",
                "source": "launchd",
                "pid": int(pid) if pid and pid.isdigit() else None,
                "bar_interval": bar_interval,
            }
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return status


def stop_bot() -> None:
    global bot_instance, bot_thread
    with bot_lock:
        if bot_instance:
            bot_instance.stop()
        thread = bot_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    with bot_lock:
        bot_instance = None
        bot_thread = None


def stop_external_bot() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{domain}/{EXTERNAL_BOT_LABEL}"],
        capture_output=True, text=True, timeout=5, check=False,
    )


def start_bot(symbols: list[str], acc_index: int, bar_interval: int = DEFAULT_BAR_INTERVAL) -> dict[str, Any]:
    global bot_instance, bot_thread
    if not symbols:
        raise ValueError("Select at least one symbol")
    if bar_interval not in BAR_INTERVAL_TYPES:
        raise ValueError(f"unsupported bar interval: {bar_interval}")
    available = accounts()
    account = next((item for item in available if int(item.get("acc_id", 0)) == PAPER_ACCOUNT_ID), None)
    if not account or str(account.get("trd_env", "")).upper() != "SIMULATE":
        raise ValueError("Required competition SIMULATE account is unavailable")
    if not PAPER_BOT_PLIST.exists():
        raise RuntimeError("paper bot LaunchAgent is missing")
    with PAPER_BOT_PLIST.open("rb") as handle:
        plist = plistlib.load(handle)
    args = list(plist.get("ProgramArguments", []))
    if "--bar-interval" in args:
        args[args.index("--bar-interval") + 1] = str(bar_interval)
    else:
        args.extend(["--bar-interval", str(bar_interval)])
    symbol_args = args[2:]
    while symbol_args and not symbol_args[0].startswith("--"):
        args.pop(2)
        symbol_args = args[2:]
    args[2:2] = [str(symbols[0]).upper().removeprefix("US.")]
    plist["ProgramArguments"] = args
    with PAPER_BOT_PLIST.open("wb") as handle:
        plistlib.dump(plist, handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{EXTERNAL_BOT_LABEL}"], capture_output=True, text=True, timeout=5, check=False)
    loaded = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PAPER_BOT_PLIST)], capture_output=True, text=True, timeout=5, check=False)
    if loaded.returncode != 0:
        fallback = subprocess.run(["/bin/launchctl", "load", "-w", str(PAPER_BOT_PLIST)], capture_output=True, text=True, timeout=5, check=False)
        if fallback.returncode != 0:
            raise RuntimeError(loaded.stderr.strip() or fallback.stderr.strip() or "could not load paper bot")
    subprocess.run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{EXTERNAL_BOT_LABEL}"], capture_output=True, text=True, timeout=5, check=False)
    time.sleep(1)
    return execution_status()


def do_backtest(payload: dict[str, Any], progress=None) -> dict[str, Any]:
    raw_symbol = str(payload.get("symbol") or payload.get("data_path") or SYMBOL).upper().strip()
    symbol = "US.APP" if raw_symbol in {"", "APP", "US.APP"} else raw_symbol
    if symbol != SYMBOL:
        raise ValueError("This dashboard is configured for US.APP")
    today = pd.Timestamp.now().normalize()
    requested_end = normalize_date(payload.get("end", today.strftime("%Y-%m-%d")), "end")
    requested_end_date = pd.Timestamp(min(requested_end, today.strftime("%Y-%m-%d")))
    requested_start = normalize_date(payload.get("start", (requested_end_date - pd.Timedelta(days=MAX_BACKTEST_DAYS)).strftime("%Y-%m-%d")), "start")
    # The scored window follows the selected dates but cannot exceed 30 days.
    # Earlier candles are fetched separately for indicator warm-up.
    end = requested_end_date.strftime("%Y-%m-%d")
    start_date = pd.Timestamp(requested_start)
    if start_date >= requested_end_date:
        raise ValueError("Start date must be before end date")
    if (requested_end_date - start_date).days > MAX_BACKTEST_DAYS:
        start_date = requested_end_date - pd.Timedelta(days=MAX_BACKTEST_DAYS)
    start = start_date.strftime("%Y-%m-%d")
    interval = int(payload.get("interval", INTERVAL_MINUTES))
    if interval not in {1, 3, 5, 10, 15, 30, 60, 120, 180, 240, 1440}:
        raise ValueError("Unsupported candle interval")
    if progress:
        progress(0, 0, "Fetching Moomoo candles")
    bars_per_trading_day = max(1, 390 // interval) if interval < 1440 else 1
    warmup_trading_days = math.ceil((int(BASELINE["grid_lookback"]) + 5) / bars_per_trading_day)
    warmup_days = max(5, math.ceil(warmup_trading_days * 7 / 5))
    warmup_start = pd.Timestamp(start) - pd.Timedelta(days=warmup_days)
    fetched = fetch_moomoo_candles(symbol, warmup_start.strftime("%Y-%m-%d"), end, interval)
    capital = float(payload.get("capital", 100000))
    allocation = float(payload.get("allocation", 1.0))
    slip = float(payload.get("slippage_bps", 2))
    commission = float(payload.get("commission", 0))
    base = BASELINE.copy()
    for key in base:
        if key in payload:
            base[key] = bool(payload[key]) if isinstance(base[key], bool) else type(base[key])(payload[key])
    frame = load_candles(symbol, interval, warmup_start.strftime("%Y-%m-%d"), f"{end}T23:59:59")
    if frame.empty:
        raise ValueError("No cached Moomoo candles found after the fetch")
    if interval == 1440:
        # A daily run must never silently consume an intraday cache. Moomoo
        # timestamps can vary by session, so validate the elapsed spacing rather
        # than requiring midnight timestamps.
        unique_days = frame.index.normalize().nunique()
        median_spacing = frame.index.to_series().sort_values().diff().dropna().median()
        if unique_days < max(2, len(frame) - 1) or median_spacing < pd.Timedelta(hours=12):
            raise ValueError(
                f"Daily candle integrity check failed: received {len(frame)} rows "
                f"across {unique_days} dates with median spacing {median_spacing}. "
                "Refusing to run the daily strategy on intraday data."
            )
        if len(frame) < int(BASELINE["grid_lookback"]) + 2:
            raise ValueError("Not enough daily candles for the 45-day grid warm-up")
    frame.attrs["symbol"] = symbol
    frame.attrs["interval_minutes"] = interval
    # Every displayed result is stitched from unseen walk-forward folds. There
    # is no full-period in-sample fallback, even if the checkbox is unticked.
    if payload.get("optimize", True):
        optimisation = optimize(frame, base, capital, allocation, slip, commission, progress)
        x, trades, data = walk_forward_replay(frame, optimisation, capital, allocation, slip, commission)
    else:
        # A disabled optimiser still runs the selected daily baseline. An empty
        # fold list is not a valid backtest and previously produced a misleading
        # zero-trade report.
        optimisation = []
        x, trades, data = strategy_run(frame, base, capital, allocation, slip, commission)
    result = strategy_stats(trades, data, capital)
    images = charts(x, trades, data["equity"])
    run_id = save_backtest(symbol, interval, start, end, base, result, trades, optimisation)
    response = {
        "run_id": run_id,
        "symbol": symbol,
        "candles_fetched": fetched,
        "stats": result,
        "params": base,
        "trades": [asdict(t) for t in trades],
        "candle_interval_minutes": interval,
        "candle_count_used": len(frame),
        "requested_window": {"start": start, "end": end, "calendar_days": (pd.Timestamp(end) - pd.Timestamp(start)).days},
        "warmup_window": {"start": warmup_start.strftime("%Y-%m-%d"), "calendar_days": warmup_days},
        "charts": images,
        "optimization": optimisation,
        "note": "Strict walk-forward only: each parameter set was selected on earlier Moomoo candles and evaluated only on the following unseen fold. Causal candle-open grid crossings, same-candle open fills in research, one long position; close is plotted but never used as a signal." + (" No valid optimisation candidates met the minimum trade threshold." if not optimisation else ""),
    }
    global last_backtest
    last_backtest = response
    return response


def start_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    global backtest_job
    with backtest_lock:
        if backtest_job.get("state") == "running":
            return {"state": "running", **{k: backtest_job.get(k) for k in ("done", "total", "message", "started_at")}}
        backtest_cancel.clear()
        backtest_job = {"state": "running", "done": 0, "total": 0, "message": "Starting", "started_at": time.time()}

    def progress(done, total, message):
        if backtest_cancel.is_set():
            raise RuntimeError("backtest cancelled")
        with backtest_lock:
            backtest_job.update(done=done, total=total, message=message)

    def worker():
        global backtest_job
        try:
            result = do_backtest(payload, progress)
            with backtest_lock:
                backtest_job.update(state="complete", done=backtest_job.get("total", 0), message="Complete", result=result)
        except Exception as exc:
            if backtest_cancel.is_set():
                with backtest_lock:
                    backtest_job.update(state="idle", done=0, total=0, message="Cancelled")
                return
            logging.exception("backtest failed")
            with backtest_lock:
                backtest_job.update(state="error", message=str(exc), error=str(exc))

    threading.Thread(target=worker, name="quant-backtest", daemon=True).start()
    return {"state": "running", "done": 0, "total": 0, "message": "Starting"}


def stop_backtest() -> dict[str, Any]:
    with backtest_lock:
        if backtest_job.get("state") == "running":
            backtest_cancel.set()
            backtest_job["message"] = "Cancelling"
        return dict(backtest_job)


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Moomoo Quant Dashboard</title><style>
:root{--bg:#07101d;--panel:#101d2f;--panel2:#0c1727;--line:#243750;--text:#edf3fb;--muted:#8ea2bd;--green:#66e3ad;--blue:#83aefe;--red:#ff8e9b;--amber:#f4c66b}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07101d,#0b1b30 55%,#07101d);color:var(--text);font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}main{max-width:1600px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.eyebrow{font-size:10px;letter-spacing:.17em;color:var(--green);font-weight:700;text-transform:uppercase}h1{font-size:29px;margin:5px 0}h2{font-size:15px;margin:0 0 12px}.sub,.muted{color:var(--muted)}.layout{display:grid;grid-template-columns:310px minmax(0,1fr);gap:16px;margin-top:20px}.panel{background:rgba(16,29,47,.95);border:1px solid var(--line);border-radius:11px;padding:15px;margin-bottom:16px}.side{position:sticky;top:15px;height:max-content}.field{margin:11px 0}.field label{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}.field input,.field select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:8px}.btn{border:1px solid var(--line);background:#19304e;color:var(--text);border-radius:6px;padding:8px 11px;cursor:pointer}.btn.primary{background:#1b9b70;color:#03140d;border-color:#1b9b70;font-weight:700}.btn.stop{background:#6d2435;color:#fff;border-color:#8f3548}.buttons{display:flex;gap:7px;flex-wrap:wrap}.status{min-height:21px;color:var(--muted);margin-top:8px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:9px}.card{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:11px}.card span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.card strong{display:block;font-size:19px;margin-top:3px}.positive{color:var(--green)}.negative{color:var(--red)}.amber{color:var(--amber)}.chart{width:100%;border-radius:7px;background:#fff;margin-top:10px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.tablewrap{overflow:auto;max-height:510px}table{width:100%;border-collapse:collapse;font-size:11px}th,td{padding:7px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 7px;font-size:10px}.pill.on{color:var(--green);border-color:#287a5e}.pill.off{color:var(--red);border-color:#783443}.small{font-size:11px;color:var(--muted)}.sectionbar{display:flex;align-items:center;justify-content:space-between;gap:10px}.danger-note{border-left:3px solid var(--amber);padding-left:9px;color:var(--muted);font-size:11px}@media(max-width:1000px){.layout{grid-template-columns:1fr}.side{position:static}.grid2{grid-template-columns:1fr}.top{display:block}}
</style></head><body><main><div class="top"><div><div class="eyebrow">Local private trading workspace</div><h1>Moomoo Quant Dashboard</h1><div class="sub">Tester · Optimiser · Paper execution</div></div><div id="execBadge" class="pill off">PAPER BOT STOPPED</div></div><div class="layout"><aside class="side"><div class="panel"><h2>Tester / optimiser</h2><div class="field"><label>Moomoo OHLC file</label><input id="dataPath" placeholder="/path/to/export.csv"></div><div class="field"><label>Starting capital</label><input id="capital" type="number" value="100000"></div><div class="field"><label>Allocation per entry</label><input id="allocation" type="number" step=".01" value=".95"></div><div class="field"><label>Slippage (bps/side)</label><input id="slippage" type="number" step=".1" value="2"></div><div class="field"><label>Commission per round trip</label><input id="commission" type="number" step=".01" value="0"></div><label class="small"><input id="optimise" type="checkbox" checked> Run parameter optimisation</label><p><button class="btn primary" onclick="backtest()">Run tester</button></p><div id="testStatus" class="status"></div></div><div class="panel"><h2>Automatic paper execution</h2><div class="field"><label>US symbols (comma separated)</label><input id="symbols" placeholder="AAPL,NVDA,APP"></div><div class="field"><label>US SIMULATE account index</label><input id="accIndex" type="number" value="0" min="0"></div><div class="buttons"><button class="btn" onclick="accounts()">List accounts</button><button class="btn primary" onclick="startPaper()">Start auto-paper</button><button class="btn stop" onclick="stopPaper()">Stop</button></div><div id="accountList" class="small" style="margin-top:9px"></div><div id="paperStatus" class="status"></div><p class="danger-note">Orders are submitted automatically only with <b>TrdEnv.SIMULATE</b>. The server refuses a non-simulated account.</p></div><div class="panel"><h2>Execution log</h2><pre id="log" class="small" style="white-space:pre-wrap;max-height:180px;overflow:auto">No events yet.</pre></div></aside><section><div class="panel" id="summary"><div class="sectionbar"><h2>Tester results</h2><span class="pill off">NO RUN</span></div><p class="muted">Run the tester against a Moomoo-exported OHLC file.</p></div><div class="grid2"><div class="panel"><h2>Price / signals / trades</h2><img id="priceChart" class="chart" style="display:none"></div><div class="panel"><h2>Equity curve</h2><img id="equityChart" class="chart" style="display:none"><h2 style="margin-top:18px">Monthly returns</h2><img id="monthlyChart" class="chart" style="display:none"></div></div><div class="panel"><div class="sectionbar"><h2>Optimiser ranking</h2><span class="small">Same metrics family as the results dashboard</span></div><div class="tablewrap"><table><thead><tr><th>Rank</th><th>Return %</th><th>Max DD %</th><th>Trades</th><th>Win %</th><th>PF</th><th>Parameters</th></tr></thead><tbody id="optRows"><tr><td colspan="7" class="muted">No optimisation run.</td></tr></tbody></table></div></div><div class="panel"><h2>Trade list</h2><div class="tablewrap"><table><thead><tr><th>Entry</th><th>Entry px</th><th>Qty</th><th>Exit</th><th>Exit px</th><th>Net P/L</th><th>Return %</th><th>Reason</th></tr></thead><tbody id="tradeRows"><tr><td colspan="8" class="muted">No trades.</td></tr></tbody></table></div></div></section></div></main><script>
const $=id=>document.getElementById(id);const money=x=>Number(x).toFixed(2);function setStatus(id,msg){$(id).textContent=msg}function pct(x){return Number(x).toFixed(2)+'%'}
const oldFileField=$('dataPath');if(oldFileField){oldFileField.id='symbol';oldFileField.value='US.APP';oldFileField.placeholder='US.APP';if(oldFileField.previousElementSibling)oldFileField.previousElementSibling.textContent='Moomoo symbol';if($('symbols'))$('symbols').value='APP';oldFileField.insertAdjacentHTML('beforebegin','<div class="field"><label>Start date</label><input id="start" type="date" value="2026-01-01"></div><div class="field"><label>End date</label><input id="end" type="date" value="2026-11-13"></div><div class="field"><label>Bar interval</label><select id="interval"><option value="10" selected>10 minutes</option><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option></select></div>');}
async function backtest(){let symbol=$('symbol').value.trim();setStatus('testStatus','Fetching Moomoo candles into SQLite, then running tester and optimiser…');let body={symbol,start:$('start').value,end:$('end').value,interval:+$('interval').value,capital:+$('capital').value,allocation:+$('allocation').value,slippage_bps:+$('slippage').value,commission:+$('commission').value,optimize:$('optimise').checked};let r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json();if(!r.ok)return setStatus('testStatus',d.error);setStatus('testStatus','Complete · '+d.candles_fetched+' Moomoo candles cached · SQLite run '+d.run_id);let s=d.stats;let vals=[['Return',pct(s.total_return_pct)],['Ending equity',money(s.ending_equity)],['Max drawdown',pct(s.max_drawdown_pct)],['Trades',s.trades],['Win rate',pct(s.win_rate_pct)],['Profit factor',s.profit_factor],['Avg trade',pct(s.average_trade_pct)],['Sharpe approx',s.sharpe_approx]];$('summary').innerHTML='<div class="sectionbar"><h2>Tester results · '+d.symbol+'</h2><span class="pill on">COMPLETE</span></div><div class="small">Moomoo OpenD · '+d.candles_fetched+' candles · SQLite run '+d.run_id+'</div><div class="cards" style="margin-top:12px">'+vals.map(v=>'<div class="card"><span>'+v[0]+'</span><strong class="'+(String(v[1]).startsWith('-')?'negative':'positive')+'">'+v[1]+'</strong></div>').join('')+'</div><p class="small">'+d.note+'</p>';$('priceChart').src='data:image/png;base64,'+d.charts.price;$('priceChart').style.display='block';$('equityChart').src='data:image/png;base64,'+d.charts.equity;$('equityChart').style.display='block';$('monthlyChart').src='data:image/png;base64,'+d.charts.monthly;$('monthlyChart').style.display='block';$('optRows').innerHTML=d.optimization.map((r,i)=>'<tr><td>'+(i+1)+'</td><td class="positive">'+r.stats.total_return_pct+'</td><td class="negative">'+r.stats.max_drawdown_pct+'</td><td>'+r.stats.trades+'</td><td>'+r.stats.win_rate_pct+'</td><td>'+r.stats.profit_factor+'</td><td class="small">'+JSON.stringify(r.params)+'</td></tr>').join('')||'<tr><td colspan="7">Optimisation disabled.</td></tr>';$('tradeRows').innerHTML=d.trades.map(t=>'<tr><td>'+t.entry_time+'</td><td>'+money(t.entry_price)+'</td><td>'+t.qty+'</td><td>'+t.exit_time+'</td><td>'+money(t.exit_price)+'</td><td class="'+(t.net_pnl>=0?'positive':'negative')+'">'+money(t.net_pnl)+'</td><td>'+pct(t.return_pct)+'</td><td>'+t.reason+'</td></tr>').join('')||'<tr><td colspan="8">No trades.</td></tr>'}
async function accounts(){setStatus('paperStatus','Reading OpenD accounts…');let r=await fetch('/api/accounts');let d=await r.json();if(!r.ok)return setStatus('paperStatus',d.error);$('accountList').innerHTML=d.map((a,i)=>`<div>${i}: <b>${a.trd_env||''}</b> · ${a.acc_type||''} · ${a.sim_acc_type||''} · ${JSON.stringify(a.trdmarket_auth||'')}</div>`).join('');setStatus('paperStatus','Select the competition account index, then start.')}async function startPaper(){let symbols=$('symbols').value.split(',').map(x=>x.trim()).filter(Boolean);if(!symbols.length)return setStatus('paperStatus','Enter at least one symbol.');setStatus('paperStatus','Verifying SIMULATE account and starting bot…');let r=await fetch('/api/paper/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols,acc_index:+$('accIndex').value})});let d=await r.json();if(!r.ok)return setStatus('paperStatus',d.error);setStatus('paperStatus','Auto-paper execution is running.');refreshStatus()}async function stopPaper(){await fetch('/api/paper/stop',{method:'POST'});setStatus('paperStatus','Stopped.');refreshStatus()}async function refreshStatus(){let r=await fetch('/api/status');let d=await r.json();$('execBadge').textContent=d.active?'PAPER BOT RUNNING':'PAPER BOT STOPPED';$('execBadge').className='pill '+(d.active?'on':'off');if(d.active)$('paperStatus').textContent='Running '+d.symbols.join(', ')+' on account index '+d.account_index+' · '+d.mode;let l=await fetch('/api/log');$('log').textContent=await l.text()}setInterval(refreshStatus,3000);refreshStatus();
</script><script>const testerInterval=document.getElementById('interval');if(testerInterval){testerInterval.innerHTML='<option value="1">1 minute</option><option value="3">3 minutes</option><option value="5">5 minutes</option><option value="10">10 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option><option value="120">120 minutes</option><option value="180">180 minutes</option><option value="240">240 minutes</option><option value="1440">Daily</option>';testerInterval.value='15';}</script></body></html>'''


HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Moomoo Quant Dashboard</title>
<style>body{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#08111f;color:#eef4fb;margin:0}main{max-width:1400px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:300px 1fr;gap:16px}.panel{background:#101d2f;border:1px solid #28405e;border-radius:10px;padding:16px;margin-bottom:16px}.field{margin:10px 0}.field label{display:block;color:#9eb0c7;font-size:12px;margin-bottom:4px}input,select{width:100%;box-sizing:border-box;background:#0a1627;color:white;border:1px solid #28405e;border-radius:6px;padding:8px}.btn{border:0;border-radius:6px;padding:9px 12px;background:#1d9d70;color:#04130d;font-weight:700;cursor:pointer;margin:3px}.btn.stop{background:#842e43;color:white}.muted{color:#9eb0c7}.status{min-height:24px;color:#f2c66d;margin-top:8px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}.card{background:#0a1627;border:1px solid #28405e;border-radius:7px;padding:10px}.card span{display:block;color:#9eb0c7;font-size:10px;text-transform:uppercase}.card strong{font-size:19px}.pos{color:#6ee7b7}.neg{color:#ff8998}.chart{max-width:100%;margin-top:8px;background:white;border-radius:6px}.table{overflow:auto;max-height:450px}table{border-collapse:collapse;width:100%;font-size:11px}th,td{border-bottom:1px solid #28405e;padding:7px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:#9eb0c7;font-weight:500}@media(max-width:850px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><h1>Moomoo Quant Dashboard</h1><p class="muted">US.APP · Moomoo OpenD · daily candles · SQLite-backed tester</p>
<div class="grid"><aside><div class="panel"><h2>Tester / optimiser</h2><div class="field"><label>Symbol</label><input id="symbol" value="US.APP"></div><div class="field"><label>Start</label><input id="start" type="date" value="2026-01-01"></div><div class="field"><label>End</label><input id="end" type="date" value="2026-11-13"></div><div class="field"><label>Interval</label><select id="interval"><option value="10" selected>10 minutes</option><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option></select></div><div class="field"><label>Capital</label><input id="capital" type="number" value="100000"></div><div class="field"><label>Allocation</label><input id="allocation" type="number" step=".01" value=".95"></div><div class="field"><label>Slippage bps</label><input id="slippage" type="number" value="2"></div><label><input id="optimize" type="checkbox" checked> Optimise parameters</label><br><button class="btn" id="run">Run tester</button><div id="testStatus" class="status"></div></div>
<div class="panel"><h2>Automatic paper execution</h2><div class="field"><label>Account index</label><input id="account" type="number" value="2"></div><div class="field"><label>Bot candle interval</label><select id="botInterval"><option value="1">1 minute</option><option value="3">3 minutes</option><option value="5">5 minutes</option><option value="10">10 minutes</option><option value="15" selected>15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option><option value="120">120 minutes</option><option value="180">180 minutes</option><option value="240">240 minutes</option><option value="1440">Daily</option></select></div><button class="btn" id="startBot">Start APP paper bot</button><button class="btn stop" id="stopBot">Stop</button><div id="botStatus" class="status"></div></div></aside>
<section><div id="summary" class="panel"><h2>Results</h2><p class="muted">Click Run tester.</p></div><div class="panel"><h2>Price / trades</h2><img id="price" class="chart" hidden></div><div class="panel"><h2>Equity curve</h2><img id="equity" class="chart" hidden><h2>Monthly returns</h2><img id="monthly" class="chart" hidden></div><div class="panel"><h2>Optimiser ranking</h2><div class="table"><table><thead><tr><th>Rank</th><th>Return %</th><th>Max DD %</th><th>Trades</th><th>Win %</th><th>PF</th><th>Params</th></tr></thead><tbody id="optimiser"><tr><td colspan="7" class="muted">No optimisation yet.</td></tr></tbody></table></div></div><div class="panel"><h2>Trades</h2><div class="table"><table><thead><tr><th>Entry</th><th>Price</th><th>Qty</th><th>Exit</th><th>Price</th><th>P/L</th><th>Return</th><th>Reason</th></tr></thead><tbody id="trades"><tr><td colspan="8" class="muted">No trades yet.</td></tr></tbody></table></div></div></section></div></main>
<script>
const el=id=>document.getElementById(id);const money=x=>Number(x).toFixed(2);const pct=x=>Number(x).toFixed(2)+'%';const testerInterval=el('interval');if(testerInterval){testerInterval.innerHTML='<option value="1">1 minute</option><option value="3">3 minutes</option><option value="5">5 minutes</option><option value="10">10 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option><option value="120">120 minutes</option><option value="180">180 minutes</option><option value="240">240 minutes</option><option value="1440">Daily</option>';testerInterval.value='15';}
async function runTester(){el('testStatus').textContent='Fetching Moomoo candles and running…';const payload={symbol:el('symbol').value||'US.APP',start:el('start').value,end:el('end').value,interval:Number(el('interval').value),capital:Number(el('capital').value),allocation:Number(el('allocation').value),slippage_bps:Number(el('slippage').value),optimize:el('optimize').checked};try{const res=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await res.json();if(!res.ok)throw Error(d.error||'Tester failed');const s=d.stats;const vals=[['Return',pct(s.total_return_pct)],['Ending equity',money(s.ending_equity)],['Max drawdown',pct(s.max_drawdown_pct)],['Trades',s.trades],['Win rate',pct(s.win_rate_pct)],['Profit factor',s.profit_factor],['Avg trade',pct(s.average_trade_pct)],['Sharpe',s.sharpe_approx]];el('summary').innerHTML='<h2>US.APP tester results</h2><p class="muted">Moomoo candles: '+d.candles_fetched+' · SQLite run: '+d.run_id+'</p><div class="cards">'+vals.map(v=>'<div class="card"><span>'+v[0]+'</span><strong class="'+(String(v[1]).startsWith('-')?'neg':'pos')+'">'+v[1]+'</strong></div>').join('')+'</div>';for(const [id,key] of [['price','price'],['equity','equity'],['monthly','monthly']]){el(id).src='data:image/png;base64,'+d.charts[key];el(id).hidden=false}el('optimiser').innerHTML=d.optimization.length?d.optimization.map((r,i)=>'<tr><td>'+(i+1)+'</td><td>'+r.stats.total_return_pct+'</td><td>'+r.stats.max_drawdown_pct+'</td><td>'+r.stats.trades+'</td><td>'+r.stats.win_rate_pct+'</td><td>'+r.stats.profit_factor+'</td><td>'+JSON.stringify(r.params)+'</td></tr>').join(''):'<tr><td colspan="7">No optimisation results.</td></tr>';el('trades').innerHTML=d.trades.map(t=>'<tr><td>'+t.entry_time+'</td><td>'+money(t.entry_price)+'</td><td>'+t.qty+'</td><td>'+t.exit_time+'</td><td>'+money(t.exit_price)+'</td><td>'+money(t.net_pnl)+'</td><td>'+pct(t.return_pct)+'</td><td>'+t.reason+'</td></tr>').join('')||'<tr><td colspan="8">No trades.</td></tr>';el('testStatus').textContent='Complete';}catch(e){el('testStatus').textContent=e.message;el('summary').innerHTML='<h2>Tester error</h2><p class="neg">'+e.message+'</p>'}}
async function refreshBot(){const d=await (await fetch('/api/status')).json();el('botStatus').textContent=d.active?'Running '+d.symbols.join(', ')+' · '+d.mode+' · '+(d.bar_interval||15)+'m · account '+d.account_id:'Stopped'}el('run').onclick=runTester;el('startBot').onclick=async()=>{el('botStatus').textContent='Starting…';const r=await fetch('/api/paper/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols:['APP'],acc_index:Number(el('account').value),bar_interval:Number(el('botInterval').value)})});const d=await r.json();el('botStatus').textContent=r.ok?'Running '+d.symbols.join(', ')+' · '+d.mode+' · '+(d.bar_interval||15)+'m · account '+d.account_id:(d.error||'Start failed')};el('stopBot').onclick=async()=>{await fetch('/api/paper/stop',{method:'POST'});refreshBot()};refreshBot();setInterval(refreshBot,5000);
</script></body></html>'''


HTML = HTML.replace("</script></body></html>", "</script><script>el('run').onclick=async function(){el('testStatus').textContent='Starting Moomoo fetch...';const p={symbol:el('symbol').value||'US.APP',start:el('start').value,end:el('end').value,interval:Number(el('interval').value),capital:Number(el('capital').value),allocation:Number(el('allocation').value),slippage_bps:Number(el('slippage').value),optimize:el('optimize').checked};try{const r=await fetch('/api/backtest/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const a=await r.json();if(a.state==='error')throw Error(a.error);let s;do{await new Promise(x=>setTimeout(x,1000));s=await (await fetch('/api/backtest/status')).json();el('testStatus').textContent=s.total?s.message+' · '+s.done+'/'+s.total:'Fetching Moomoo candles...'}while(s.state==='running');if(s.state==='error')throw Error(s.error||s.message);el('testStatus').textContent='Complete · SQLite run '+s.result.run_id;el('summary').innerHTML='<h2>US.APP tester results</h2><div class=\"cards\">'+[['Return',s.result.stats.total_return_pct+'%'],['Max drawdown',s.result.stats.max_drawdown_pct+'%'],['Trades',s.result.stats.trades],['Win rate',s.result.stats.win_rate_pct+'%'],['Profit factor',s.result.stats.profit_factor]].map(v=>'<div class=\"card\"><span>'+v[0]+'</span><strong>'+v[1]+'</strong></div>').join('')+'</div>';for(const [id,key] of [['price','price'],['equity','equity'],['monthly','monthly']]){el(id).src='data:image/png;base64,'+s.result.charts[key];el(id).hidden=false}el('optimiser').innerHTML=s.result.optimization.map((r,i)=>'<tr><td>'+(i+1)+'</td><td>'+r.stats.total_return_pct+'</td><td>'+r.stats.max_drawdown_pct+'</td><td>'+r.stats.trades+'</td><td>'+r.stats.win_rate_pct+'</td><td>'+r.stats.profit_factor+'</td><td>'+JSON.stringify(r.params)+'</td></tr>').join('')}catch(e){el('testStatus').textContent=e.message}};</script></body></html>")

HTML = HTML.replace('<div class="panel"><h2>Automatic paper execution</h2>', '<div class="panel" hidden><h2>Automatic paper execution</h2>')

HTML = HTML.replace('value="2026-01-01"', 'value="2025-11-12"').replace('value="2026-11-13"', 'value="2026-11-12"').replace('value="2025-10-01"', 'value="2025-11-12"').replace('value="2026-11-11"', 'value="2026-11-12"').replace('value=".95"', 'value="1.0"')
HTML = HTML.replace('<button class="btn" id="run">Run tester</button>', '<button class="btn" id="run">Run tester</button><button class="btn stop" id="stopBacktest">Stop tester</button>')
HTML = HTML.replace('</script></body></html>', '</script><script>document.getElementById("stopBacktest").onclick=async()=>{document.getElementById("testStatus").textContent="Cancelling…";await fetch("/api/backtest/stop",{method:"POST"});};</script></body></html>')
HTML = HTML.replace('</body></html>', '<script>const endInput=document.getElementById("end"),startInput=document.getElementById("start"),intervalInput=document.getElementById("interval");function setMaxWindow(){const end=new Date();const start=new Date(end);start.setFullYear(start.getFullYear()-1);const iso=d=>d.toISOString().slice(0,10);if(endInput)endInput.value=iso(end);if(startInput)startInput.value=iso(start);}setMaxWindow();if(intervalInput)intervalInput.addEventListener("change",setMaxWindow);</script></body></html>')


HTML = HTML.replace('</body></html>', '<script>const maxEnd=new Date(),maxStart=new Date(maxEnd);maxStart.setDate(maxStart.getDate()-30);const toISO=d=>d.toISOString().slice(0,10);if(document.getElementById("end"))document.getElementById("end").value=toISO(maxEnd);if(document.getElementById("start"))document.getElementById("start").value=toISO(maxStart);if(document.getElementById("interval"))document.getElementById("interval").value="10";if(document.getElementById("botInterval"))document.getElementById("botInterval").value="10";</script></body></html>')


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body = json.dumps(value, default=str).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/": self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(HTML.encode())
            elif path == "/api/status": self.send_json(execution_status())
            elif path == "/api/backtest/status":
                with backtest_lock:
                    self.send_json(dict(backtest_job))
            elif path == "/api/accounts": self.send_json(accounts())
            elif path == "/api/strategy": self.send_json({"name": strategy_api.name, "version": strategy_api.version, "params": BASELINE})
            elif path == "/api/strategy/signal":
                query = dict(item.split("=", 1) for item in urlparse(self.path).query.split("&") if "=" in item)
                symbol = query.get("symbol", SYMBOL); interval = int(query.get("interval", INTERVAL_MINUTES))
                frame = data_api.candles(symbol, interval, query.get("start", "2025-01-01"), query.get("end", "2099-01-01"))
                self.send_json(strategy_api.signal(frame))
            elif path == "/api/health": self.send_json({"ok": True, "strategy": strategy_api.version, "paper_only": True})
            elif path == "/api/log":
                log_path = ROOT / "competition_bot.log"
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(log_path.read_text()[-12000:].encode() if log_path.exists() else b"No log yet.")
            else: self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.body() if path in {"/api/backtest", "/api/backtest/start", "/api/paper/start", "/api/risk"} else {}
            if path == "/api/backtest/start": self.send_json(start_backtest(payload))
            elif path == "/api/backtest/stop": self.send_json(stop_backtest())
            elif path == "/api/backtest": self.send_json(do_backtest(payload))
            elif path == "/api/paper/start": self.send_json(start_bot([str(x) for x in payload.get("symbols", [])], int(payload.get("acc_index", 0)), int(payload.get("bar_interval", DEFAULT_BAR_INTERVAL))))
            elif path == "/api/risk": self.send_json(risk_json(**payload))
            elif path == "/api/paper/stop": stop_bot(); stop_external_bot(); self.send_json(execution_status())
            else: self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            logging.exception("dashboard request failed")
            self.send_json({"error": str(exc)}, 400)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with INSTANCE_LOCK_PATH.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Dashboard already running at http://127.0.0.1:{PORT}; refusing a second instance.")
            return
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        print(f"Moomoo Quant Dashboard: http://127.0.0.1:{PORT}")
        print("Paper execution is locked to TrdEnv.SIMULATE and starts only from the dashboard.")
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: stop_bot(); server.server_close(); fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
