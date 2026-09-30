"""
Real-time-ish market movers dashboard.

Runs a small local Flask server that:
  - subscribes to Finnhub's free real-time trades WEBSOCKET for every symbol in
    WATCHLIST (config.py), aggregating live trade ticks into 2-minute OHLCV
    candles entirely in memory (no paid intraday-candle REST endpoint needed),
  - separately polls Finnhub's free /quote REST endpoint on a rotating schedule
    just to seed/refresh price, prev-close, and day open/high/low (and as a
    fallback if the websocket can't connect),
  - periodically pulls Yahoo Finance's unofficial gainers/losers/most-active
    screener and adds anything newly discovered into the same tracked
    universe, so "Top movers" reflects the whole market, not just WATCHLIST,
  - runs a rule-based buy/sell signal engine (moving averages, VWAP, RSI,
    volume) over those 2-min candles for every symbol, and flags fresh
    flips so the frontend can flash them,
  - periodically saves WATCHLIST symbols' candle history to candle_cache.json
    and reloads it on startup (if recent enough), so a restart to pick up a
    code change doesn't reset the signal engine's ~24-min warmup every time,
  - periodically pulls official, public Dow Jones/WSJ RSS headline feeds
    (no login, not a paywall workaround -- full articles still require your
    own WSJ subscription in your own browser) for a general market-news panel,
  - serves a single-page dashboard that polls this server's own /api/state
    endpoint every few seconds and renders every ticker's candlestick chart at
    once, with no click-through required to see a chart -- clicking a card
    instead opens a larger, interactive view with a hover crosshair/tooltip.

See README.md for setup. Run with:  python app.py
"""

import collections
import datetime
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
import zoneinfo

import requests
import websocket
from flask import Flask, jsonify, render_template, request

import config

app = Flask(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
FINNHUB_WS = "wss://ws.finnhub.io"
NY_TZ = zoneinfo.ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Shared in-memory state (single-process; fine for a personal local dashboard)
# ---------------------------------------------------------------------------

state_lock = threading.Lock()


def _make_symbol_entry(symbol, name, is_etf):
    return {
        "symbol": symbol,
        "name": name,
        "is_etf": is_etf,
        "price": None,
        "prev_close": None,
        "change": None,
        "percent": None,
        "high": None,
        "low": None,
        "open": None,
        "candles": collections.deque(maxlen=config.HISTORY_MAX_POINTS),
        "_current_candle": None,  # {"bucket": int, "o","h","l","c","vol": float}
        "last_update": None,
        "last_tick_source": None,  # "websocket" | "rest"
        "error": None,
        "dynamic": False,  # True if added at runtime via the search box, not config.WATCHLIST
    }


symbols_state = {
    row["symbol"]: _make_symbol_entry(row["symbol"], row["name"], row["is_etf"]) for row in config.WATCHLIST
}


# ---------------------------------------------------------------------------
# Candle history persistence (config.PERSIST_CANDLES) -- covers EVERY
# currently-tracked symbol (WATCHLIST and anything the movers screener or
# search box discovered) plus the screener's own bookkeeping (which slots are
# used, how long each has been held). Saves periodically and reloads on
# startup if recent enough, so restarting the app to pick up a code change
# doesn't reset the signal engine's ~24-min warmup, or the movers screener's
# eviction-protection clock, every single time. Without this, a fast
# iteration cycle of restarts (exactly what happens while actively developing
# this dashboard) can leave the screener's 20 slots permanently full of
# symbols too young to evict, freezing out real new movers indefinitely --
# that's what was happening when INTC's 7% move didn't show up: the cap was
# full, nothing was old enough to evict, and every restart reset that clock
# back to zero before it could ever elapse.
# candle_cache.json is created next to app.py and is safe to delete any time
# (worst case: warmup starts over, same as before this existed).
# ---------------------------------------------------------------------------

CANDLE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candle_cache.json")


def _save_candle_cache():
    if not config.PERSIST_CANDLES:
        return
    try:
        with state_lock:
            symbols_payload = {}
            for sym, s in symbols_state.items():
                symbols_payload[sym] = {
                    "candles": list(s["candles"]),
                    "current_candle": s["_current_candle"],
                    "prev_close": s["prev_close"],
                    "name": s["name"],
                    "is_etf": s["is_etf"],
                    "dynamic": s["dynamic"],
                }
        with screener_added_lock:
            payload = {
                "_saved_at": time.time(),
                "_screener_added_symbols": list(screener_added_symbols),
                "_screener_added_at": dict(screener_added_at),
                "_screener_last_seen": dict(screener_last_seen),
                "symbols": symbols_payload,
            }
        tmp_path = CANDLE_CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, CANDLE_CACHE_PATH)  # atomic replace on both POSIX and Windows
    except Exception as exc:  # noqa: BLE001 -- persistence is a nice-to-have, never fatal
        print(f"[candle-cache] save failed (non-fatal): {exc}")


def _load_candle_cache():
    if not config.PERSIST_CANDLES:
        return
    try:
        with open(CANDLE_CACHE_PATH, "r") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print("[candle-cache] no cache file yet -- normal warm-up this run")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[candle-cache] load failed, starting fresh (non-fatal): {exc}")
        return

    age = time.time() - payload.get("_saved_at", 0)
    if age > config.CANDLE_CACHE_MAX_AGE_SECONDS:
        print(
            f"[candle-cache] cache is {age / 60:.0f} min old (limit "
            f"{config.CANDLE_CACHE_MAX_AGE_SECONDS / 60:.0f} min) -- too stale to trust, "
            f"discarding. Normal warm-up this run."
        )
        return

    restored = 0
    with state_lock:
        for sym, cached in (payload.get("symbols") or {}).items():
            s = symbols_state.get(sym)
            if s is None:
                # Not a current WATCHLIST symbol -- it was screener/search
                # discovered last run. Recreate its entry so its candle
                # history (and the screener bookkeeping restored below)
                # isn't wasted just because of the restart.
                s = _make_symbol_entry(sym, cached.get("name", sym), cached.get("is_etf", False))
                s["dynamic"] = True
                symbols_state[sym] = s
            s["candles"] = collections.deque(cached.get("candles") or [], maxlen=config.HISTORY_MAX_POINTS)
            s["_current_candle"] = cached.get("current_candle")
            if cached.get("prev_close"):
                s["prev_close"] = cached["prev_close"]
            restored += 1

    with screener_added_lock:
        still_tracked = set(symbols_state.keys())
        screener_added_symbols.update(s for s in (payload.get("_screener_added_symbols") or []) if s in still_tracked)
        screener_added_at.update(
            {k: v for k, v in (payload.get("_screener_added_at") or {}).items() if k in still_tracked}
        )
        screener_last_seen.update(
            {k: v for k, v in (payload.get("_screener_last_seen") or {}).items() if k in still_tracked}
        )

    print(
        f"[candle-cache] restored candle history for {restored} symbols "
        f"({len(screener_added_symbols)} were screener-tracked) -- cache was {age / 60:.1f} min old"
    )


def persist_candles_forever():
    if not config.PERSIST_CANDLES:
        return
    while True:
        time.sleep(config.CANDLE_CACHE_SAVE_INTERVAL_SECONDS)
        _save_candle_cache()

# The live websocket connection (if currently connected), so a request thread
# handling /api/track can subscribe a newly-added symbol immediately instead
# of waiting for the next reconnect.
current_ws_app = None
current_ws_lock = threading.Lock()

server_meta = {
    "started_at": time.time(),
    "last_quote_cycle_finished": None,
    "websocket_status": "not started",
    "movers_screener_status": "not started",
    "news_status": "not started",
    "api_key_configured": bool(config.FINNHUB_API_KEY),
}


# ---------------------------------------------------------------------------
# Simple rolling-window rate limiter for REST calls only.
# ---------------------------------------------------------------------------

class RollingRateLimiter:
    def __init__(self, max_calls, window_seconds=60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls = collections.deque()
        self.lock = threading.Lock()

    def wait_for_slot(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.window_seconds:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.window_seconds - (now - self.calls[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


rate_limiter = RollingRateLimiter(config.MAX_CALLS_PER_MINUTE)

# A separate, small budget just for on-demand symbol lookups (the search
# box's "track any ticker" feature). Without this, a lookup request has to
# wait in line behind whatever the continuously-running background poll loop
# is doing, which can make a search feel "stuck" for a long time whenever the
# main budget is momentarily saturated.
track_rate_limiter = RollingRateLimiter(config.TRACK_CALLS_PER_MINUTE)

# A 429 from Finnhub means the account (not just one of our two local
# limiters) is over its real limit -- e.g. the same API key is being used by
# more than one running copy of this app, so the two limiters' budgets add up
# to more than Finnhub actually allows for the account. When that happens,
# pause ALL outgoing calls (both limiters) for a cooldown window instead of
# hammering the API again next cycle and getting 429'd repeatedly.
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0


def _respect_cooldown():
    while True:
        with _cooldown_lock:
            remaining = _cooldown_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def _start_cooldown(seconds):
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def finnhub_get(path, params, limiter=None):
    _respect_cooldown()
    (limiter or rate_limiter).wait_for_slot()
    resp = requests.get(
        f"{FINNHUB_BASE}{path}",
        params=params,
        headers={"X-Finnhub-Token": config.FINNHUB_API_KEY},
        timeout=10,
    )
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        cooldown_s = float(retry_after) if retry_after else 20.0
        _start_cooldown(cooldown_s)
        raise RuntimeError(
            f"429 rate-limited by Finnhub (cooling down {cooldown_s:.1f}s) -- if you have more than one "
            f"copy of this app running with the same API key, close the extra ones"
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Candle aggregation (shared by both the websocket tick handler and the REST
# fallback) -- turns a stream of (symbol, price, unix_seconds, volume) points
# into rolling OHLCV candles, bucketed at config.CANDLE_BUCKET_SECONDS (2 min
# by default). Volume is only ever real when it comes from the websocket
# trade feed (each trade message includes size traded) -- REST-sourced ticks
# pass volume=None since Finnhub's /quote endpoint has no per-tick size, so
# volume-based signal logic is only meaningful while the websocket is live.
# ---------------------------------------------------------------------------

def _apply_tick(symbol, price, unix_seconds, source, volume=None):
    if symbol not in symbols_state or price in (None, 0):
        return
    bucket = int(unix_seconds // config.CANDLE_BUCKET_SECONDS)
    vol = volume or 0
    with state_lock:
        s = symbols_state[symbol]
        cur = s["_current_candle"]
        if cur is None or cur["bucket"] != bucket:
            if cur is not None:
                s["candles"].append(
                    {
                        "t": cur["bucket"] * config.CANDLE_BUCKET_SECONDS,
                        "o": cur["o"], "h": cur["h"], "l": cur["l"], "c": cur["c"], "vol": cur["vol"],
                    }
                )
            cur = {"bucket": bucket, "o": price, "h": price, "l": price, "c": price, "vol": vol}
            s["_current_candle"] = cur
        else:
            cur["h"] = max(cur["h"], price)
            cur["l"] = min(cur["l"], price)
            cur["c"] = price
            cur["vol"] += vol

        s["price"] = price
        if s["prev_close"]:
            s["change"] = price - s["prev_close"]
            s["percent"] = (s["change"] / s["prev_close"]) * 100
        s["last_update"] = time.time()
        s["last_tick_source"] = source


# ---------------------------------------------------------------------------
# Background poller: REST quotes (seeds prev-close/open/high-low; also acts
# as the fallback price source if the websocket is disabled or can't connect)
# ---------------------------------------------------------------------------

def poll_quotes_forever():
    while True:
        with state_lock:
            symbols = list(symbols_state.keys())
        for sym in symbols:
            try:
                data = finnhub_get("/quote", {"symbol": sym})
                price = data.get("c")
                prev_close = data.get("pc")
                if price in (None, 0) or prev_close in (None, 0):
                    raise ValueError("empty quote payload")
                now_ts = time.time()
                with state_lock:
                    s = symbols_state[sym]
                    s["prev_close"] = prev_close
                    s["high"] = data.get("h")
                    s["low"] = data.get("l")
                    s["open"] = data.get("o")
                    s["error"] = None
                    have_live_ticks = s["last_tick_source"] == "websocket" and s["last_update"] and (
                        now_ts - s["last_update"] < 120
                    )
                # Only let REST drive the visible price/candles when the websocket
                # isn't actively delivering ticks for this symbol -- otherwise the
                # two sources would fight over the same candle.
                if not have_live_ticks:
                    _apply_tick(sym, price, now_ts, source="rest")
            except Exception as exc:  # noqa: BLE001 - keep polling other symbols regardless
                with state_lock:
                    symbols_state[sym]["error"] = str(exc)
            # Small pacing gap between calls. The rate limiter already caps
            # the total per minute, but without this, a whole cycle's worth
            # of calls can fire in under a second -- fine against the 60/min
            # average, but the kind of instantaneous burst that trips a
            # provider-side per-second limit and causes 429s.
            time.sleep(0.15)
        server_meta["last_quote_cycle_finished"] = time.time()
        time.sleep(1)


# ---------------------------------------------------------------------------
# Background poller: Finnhub real-time trades websocket
# ---------------------------------------------------------------------------

def run_websocket_forever():
    if not config.USE_WEBSOCKET_CANDLES:
        server_meta["websocket_status"] = "disabled in config"
        return

    backoff = 2

    def on_open(ws):
        nonlocal backoff
        global current_ws_app
        backoff = 2
        server_meta["websocket_status"] = "connected"
        with current_ws_lock:
            current_ws_app = ws
        with state_lock:
            symbols = list(symbols_state.keys())
        for sym in symbols:
            ws.send(json.dumps({"type": "subscribe", "symbol": sym}))

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except ValueError:
            return
        if msg.get("type") != "trade":
            return
        for tick in msg.get("data", []) or []:
            sym = tick.get("s")
            price = tick.get("p")
            ts_ms = tick.get("t")
            if sym is None or price is None or ts_ms is None:
                continue
            _apply_tick(sym, price, ts_ms / 1000.0, source="websocket", volume=tick.get("v"))

    def on_error(ws, error):
        server_meta["websocket_status"] = f"error: {error}"

    def on_close(ws, close_status_code, close_msg):
        global current_ws_app
        server_meta["websocket_status"] = "disconnected, reconnecting…"
        with current_ws_lock:
            if current_ws_app is ws:
                current_ws_app = None

    while True:
        try:
            ws_app = websocket.WebSocketApp(
                f"{FINNHUB_WS}?token={config.FINNHUB_API_KEY}",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:  # noqa: BLE001
            server_meta["websocket_status"] = f"crashed: {exc}"
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Dynamically track a new ticker typed into the search box -- validates it
# against Finnhub, adds it to symbols_state, and subscribes it on the live
# websocket (if connected) so it starts getting real-time ticks immediately.
# ---------------------------------------------------------------------------

def ensure_symbol_tracked(raw_symbol):
    symbol = (raw_symbol or "").strip().upper()
    if not symbol or len(symbol) > 10:
        return {"ok": False, "error": "not a valid ticker"}

    with state_lock:
        already = symbol in symbols_state
    if already:
        return {"ok": True, "symbol": symbol, "already_tracked": True}

    try:
        quote = finnhub_get("/quote", {"symbol": symbol}, limiter=track_rate_limiter)
        price = quote.get("c")
        if price in (None, 0):
            return {"ok": False, "error": f"'{symbol}' isn't a recognized ticker on Finnhub"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    name = symbol
    is_etf = False
    try:
        profile = finnhub_get("/stock/profile2", {"symbol": symbol}, limiter=track_rate_limiter)
        if profile.get("name"):
            name = profile["name"]
        # Rough heuristic: Finnhub's company profile is largely empty for ETFs
        # (no industry, no IPO date) since they're funds, not companies.
        if profile and not profile.get("finnhubIndustry") and not profile.get("ipo"):
            is_etf = True
    except Exception:  # noqa: BLE001 - profile is a nice-to-have, not required
        pass

    with state_lock:
        if symbol not in symbols_state:
            symbols_state[symbol] = _make_symbol_entry(symbol, name, is_etf)
            symbols_state[symbol]["dynamic"] = True
        # Set prev-close/open/high/low BEFORE the first tick, so _apply_tick
        # below has a prev_close to compute the % change against right away
        # instead of waiting a cycle.
        symbols_state[symbol]["prev_close"] = quote.get("pc")
        symbols_state[symbol]["high"] = quote.get("h")
        symbols_state[symbol]["low"] = quote.get("l")
        symbols_state[symbol]["open"] = quote.get("o")

    with current_ws_lock:
        ws = current_ws_app
    if ws is not None:
        try:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
        except Exception:  # noqa: BLE001 - REST polling will still pick it up next cycle
            pass

    # Seed the first candle/price immediately rather than waiting for the
    # next REST poll cycle.
    _apply_tick(symbol, price, time.time(), source="rest")

    return {"ok": True, "symbol": symbol, "name": name, "is_etf": is_etf, "already_tracked": False}


# ---------------------------------------------------------------------------
# Market-wide movers screener (config.USE_MOVERS_SCREENER) -- periodically
# pulls Yahoo Finance's unofficial day_gainers/day_losers/most_actives lists
# and feeds anything new through ensure_symbol_tracked() above, so it joins
# the same tracked universe as WATCHLIST and shows up ranked in Top Movers /
# Gainers / Losers with real Finnhub price/candle/signal data. UNOFFICIAL --
# see the comment above config.USE_MOVERS_SCREENER for the tradeoffs.
# ---------------------------------------------------------------------------

screener_added_symbols = set()
# When a screener-added symbol last showed up in Yahoo's gainers/losers/most-
# active lists -- used to evict the coldest one when the cap is full and a
# genuinely new mover shows up, instead of silently dropping the new mover
# (this is what happened with MRNA's 90% move getting missed once the cap
# filled up with earlier, now-cooled-off names).
screener_last_seen = {}
# When each screener-added symbol was first tracked -- used to protect it
# from eviction until it's had config.MOVERS_MIN_TRACK_SECONDS to build up
# candle history. Without this, a symbol can get evicted before the signal
# engine ever has enough bars to compute a real BUY/SELL for it (needs
# SIGNAL_MA_SLOW_PERIOD bars), which defeats the point of discovering it in
# the first place -- observed happening for real: 63 symbols added and 43
# evicted within ~16 minutes of runtime, faster than any of them could warm up.
screener_added_at = {}
screener_added_lock = threading.Lock()


def _untrack_symbol(symbol):
    """Drops a screener-added symbol to make room for a hotter one. Never
    called on WATCHLIST or search-box-added symbols -- see poll_movers_forever."""
    with current_ws_lock:
        ws = current_ws_app
    if ws is not None:
        try:
            ws.send(json.dumps({"type": "unsubscribe", "symbol": symbol}))
        except Exception:  # noqa: BLE001
            pass
    with state_lock:
        symbols_state.pop(symbol, None)


def _symbol_has_warmed_up(symbol):
    """True once a symbol has enough candles for the signal engine to have
    possibly fired a real BUY/SELL (see compute_signals()'s ma_slow gate) --
    the actual thing eviction eligibility should be protecting, not just
    elapsed wall-clock time (which candle persistence can outrun anyway)."""
    with state_lock:
        s = symbols_state.get(symbol)
        if s is None:
            return True  # already gone somehow -- nothing left to protect
        candle_count = len(s["candles"]) + (1 if s["_current_candle"] is not None else 0)
    return candle_count >= config.SIGNAL_MA_SLOW_PERIOD


def poll_movers_forever():
    if not config.USE_MOVERS_SCREENER:
        server_meta["movers_screener_status"] = "disabled in config"
        return

    try:
        from yahooquery import Screener
    except ImportError:
        server_meta["movers_screener_status"] = (
            "yahooquery not installed -- run: pip install yahooquery (or py -m pip install yahooquery "
            "on Windows), then restart the app. Top Movers will only rank WATCHLIST until then."
        )
        print(f"[movers] {server_meta['movers_screener_status']}")
        return

    screener = Screener()
    categories = ["day_gainers", "day_losers", "most_actives"]
    print(
        f"[movers] screener enabled -- polling Yahoo's {categories} every "
        f"{config.MOVERS_SCREENER_POLL_SECONDS}s, cap={config.MOVERS_MAX_DYNAMIC_SYMBOLS} new symbols"
    )

    while True:
        try:
            data = screener.get_screeners(categories, count=config.MOVERS_SCREENER_COUNT)
            discovered = []
            seen_this_cycle = set()
            for name in categories:
                result = (data or {}).get(name)
                if not result or not result.get("quotes"):
                    continue
                for q in result["quotes"]:
                    sym = q.get("symbol")
                    if sym and sym not in seen_this_cycle:
                        discovered.append(sym)
                        seen_this_cycle.add(sym)

            now = time.time()
            with screener_added_lock:
                for sym in discovered:
                    if sym in screener_added_symbols:
                        screener_last_seen[sym] = now

            added, evicted, skipped = [], [], []
            for sym in discovered:
                with state_lock:
                    already_tracked = sym in symbols_state
                if already_tracked:
                    continue  # already part of the tracked universe -- nothing to do, no cap slot spent

                with screener_added_lock:
                    room = len(screener_added_symbols) < config.MOVERS_MAX_DYNAMIC_SYMBOLS
                    candidates = list(screener_added_symbols) if not room else []

                victim = None
                if not room:
                    # Only symbols that have either already warmed up (real
                    # candle-based check, survives restarts thanks to candle
                    # persistence) or maxed out the backstop timer (catches a
                    # symbol that somehow never got a single tick) are
                    # eligible to be evicted -- a symbol still within its
                    # warmup keeps its slot even if it's gone cold, so it at
                    # least gets a chance to fire a signal before losing it.
                    eligible = [
                        s for s in candidates
                        if now - screener_added_at.get(s, 0) >= config.MOVERS_MIN_TRACK_SECONDS
                        or _symbol_has_warmed_up(s)
                    ]
                    if eligible:
                        victim = min(eligible, key=lambda s: screener_last_seen.get(s, 0))

                if not room:
                    if victim is None:
                        skipped.append(sym)  # cap full and everyone's still within their warmup window
                        continue
                    _untrack_symbol(victim)
                    with screener_added_lock:
                        screener_added_symbols.discard(victim)
                        screener_last_seen.pop(victim, None)
                        screener_added_at.pop(victim, None)
                    evicted.append(victim)

                result = ensure_symbol_tracked(sym)
                if result.get("ok") and not result.get("already_tracked"):
                    with screener_added_lock:
                        screener_added_symbols.add(sym)
                        screener_last_seen[sym] = now
                        screener_added_at[sym] = now
                    added.append(sym)

            with screener_added_lock:
                slots_used = len(screener_added_symbols)
            server_meta["movers_screener_status"] = (
                f"ok -- {len(discovered)} candidates from Yahoo, {len(added)} newly added, "
                f"{len(evicted)} evicted to make room, {len(skipped)} skipped "
                f"({slots_used}/{config.MOVERS_MAX_DYNAMIC_SYMBOLS} screener slots used)"
            )
            if added or evicted:
                print(
                    f"[movers] +{added or '[]'}  "
                    f"{'evicted ' + str(evicted) + '  ' if evicted else ''}"
                    f"slots={slots_used}/{config.MOVERS_MAX_DYNAMIC_SYMBOLS}"
                )
        except Exception as exc:  # noqa: BLE001 -- keep retrying; this is a best-effort feed
            server_meta["movers_screener_status"] = f"error: {exc}"
            print(f"[movers] error: {exc}")

        time.sleep(config.MOVERS_SCREENER_POLL_SECONDS)


# ---------------------------------------------------------------------------
# General market news (config.USE_MARKET_NEWS) -- pulls OFFICIAL, PUBLIC
# Dow Jones/WSJ RSS headline feeds (config.NEWS_RSS_FEEDS). No login, no
# scraping, not a paywall workaround -- these are feeds Dow Jones publishes
# for syndication. Headline + link only; the linked article still requires
# your own WSJ subscription in your own browser for full text. This is a
# general market-news feed, not tied to any one symbol (Finnhub's free
# /company-news endpoint would be the per-symbol equivalent, not wired in).
# ---------------------------------------------------------------------------

market_news = []  # most-recent-first list of {"title","link","source","published"}
market_news_lock = threading.Lock()


def _parse_rss_items(xml_bytes, source_label):
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        if title_el is None or not (title_el.text or "").strip():
            continue
        items.append(
            {
                "title": title_el.text.strip(),
                "link": (link_el.text or "").strip() if link_el is not None else "",
                "source": source_label,
                "published": (pubdate_el.text or "").strip() if pubdate_el is not None else "",
            }
        )
    return items


def poll_market_news_forever():
    if not config.USE_MARKET_NEWS:
        server_meta["news_status"] = "disabled in config"
        return

    while True:
        try:
            all_items = []
            errors = []
            for feed_url in config.NEWS_RSS_FEEDS:
                try:
                    resp = requests.get(feed_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                    all_items.extend(_parse_rss_items(resp.content, feed_url))
                except Exception as exc:  # noqa: BLE001 -- one bad feed shouldn't kill the others
                    errors.append(f"{feed_url}: {exc}")

            # De-dupe by link (feeds overlap) while preserving feed order, then
            # cap to NEWS_MAX_ITEMS. RSS feeds are already newest-first.
            seen_links = set()
            deduped = []
            for it in all_items:
                if it["link"] and it["link"] in seen_links:
                    continue
                seen_links.add(it["link"])
                deduped.append(it)
            deduped = deduped[: config.NEWS_MAX_ITEMS]

            with market_news_lock:
                market_news[:] = deduped

            status = f"ok -- {len(deduped)} headlines from {len(config.NEWS_RSS_FEEDS)} feed(s)"
            if errors:
                status += f", {len(errors)} feed(s) failed: {'; '.join(errors)}"
            server_meta["news_status"] = status
        except Exception as exc:  # noqa: BLE001 -- keep retrying; best-effort feed
            server_meta["news_status"] = f"error: {exc}"
            print(f"[news] error: {exc}")

        time.sleep(config.NEWS_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Buy/sell signal engine -- a RULE-BASED heuristic (NOT a trained AI/ML
# model, NOT a backtested strategy, NOT financial advice) that combines a few
# common technical indicators computed on the 2-minute candles above:
#   - trend:  fast EMA vs slow EMA (is the short-term trend up or down)
#   - value:  price vs VWAP (rich or cheap relative to today's volume-weighted
#             average -- note this is cumulative over whatever candle history
#             is currently retained, not a true session-open reset, since the
#             dashboard may not have been running since market open)
#   - momentum: RSI (overbought/oversold/trending)
#   - confirmation: current volume vs its recent average (a move on light
#             volume counts for less than the same move on heavy volume --
#             only meaningful while the websocket feed is live, since REST
#             quotes carry no per-tick size)
# Each indicator contributes a small +/- score; crossing a combined threshold
# flips the bar to "buy" or "sell". Every signal shows its own reasons so
# nothing is a black box. Thresholds/periods are tunable in config.py.
#
# This is a simple, transparent scoring system for a personal tool -- treat
# it as one input among several, not something to trade on blindly. Day
# trading carries real risk of loss.
# ---------------------------------------------------------------------------

def _ema_series(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    ema = seed
    for i in range(period, n):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def _rsi_series(closes, period):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def _rsi_from(g, l):  # noqa: E741 - short names read fine locally here
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _vwap_series(candles):
    out = [None] * len(candles)
    cum_pv = 0.0
    cum_vol = 0.0
    for i, c in enumerate(candles):
        vol = c.get("vol") or 0
        typical = (c["h"] + c["l"] + c["c"]) / 3.0
        if vol > 0:
            cum_pv += typical * vol
            cum_vol += vol
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


def _volume_ratio_series(candles, period):
    vols = [c.get("vol") or 0 for c in candles]
    n = len(vols)
    out = [None] * n
    for i in range(period, n):
        avg = sum(vols[i - period:i]) / period
        out[i] = (vols[i] / avg) if avg > 0 else None
    return out


def compute_signals(candles):
    """Pure function: given a list of 2-min OHLCV candle dicts (oldest ->
    newest, as returned by /api/state), returns (indicators, signal) where
    indicators holds a full per-bar series (for drawing lines/markers on the
    chart) and signal summarizes just the latest bar (for the badge/flash).
    Cheap enough to call fresh on every request -- no caching needed."""
    n = len(candles)
    if n == 0:
        return (
            {"ma_fast": [], "ma_slow": [], "vwap": [], "rsi": [], "vol_ratio": [], "state": []},
            {"state": "insufficient_data", "score": 0, "reasons": [], "changed_at": None, "is_fresh": False},
        )

    closes = [c["c"] for c in candles]
    ma_fast = _ema_series(closes, config.SIGNAL_MA_FAST_PERIOD)
    ma_slow = _ema_series(closes, config.SIGNAL_MA_SLOW_PERIOD)
    rsi = _rsi_series(closes, config.SIGNAL_RSI_PERIOD)
    vwap = _vwap_series(candles)
    vol_ratio = _volume_ratio_series(candles, config.SIGNAL_VOLUME_AVG_PERIOD)

    states = [None] * n
    scores = [0.0] * n
    reasons_by_bar = [None] * n

    for i in range(n):
        if ma_fast[i] is None or ma_slow[i] is None:
            states[i] = "insufficient_data"
            continue
        price = closes[i]
        score = 0.0
        reasons = []

        if ma_fast[i] > ma_slow[i]:
            score += 1
            reasons.append(f"{config.SIGNAL_MA_FAST_PERIOD}-bar MA above {config.SIGNAL_MA_SLOW_PERIOD}-bar MA")
        elif ma_fast[i] < ma_slow[i]:
            score -= 1
            reasons.append(f"{config.SIGNAL_MA_FAST_PERIOD}-bar MA below {config.SIGNAL_MA_SLOW_PERIOD}-bar MA")

        if vwap[i] is not None:
            if price > vwap[i]:
                score += 1
                reasons.append("price above VWAP")
            elif price < vwap[i]:
                score -= 1
                reasons.append("price below VWAP")

        if rsi[i] is not None:
            # Momentum-confirming, not contrarian: RSI here agrees with the
            # trend rules above rather than fighting them. A classic
            # mean-reversion read (RSI>70 = "overbought, sell") would cancel
            # out a genuine strong rally that MA/VWAP already flagged bullish
            # -- exactly the kind of move a day trader riding momentum wants
            # a BUY on, not a neutral. So high RSI = bullish confirmation,
            # low RSI = bearish confirmation, scaled by how extreme it is.
            r = rsi[i]
            if r >= 70:
                score += 1
                reasons.append(f"RSI strong bullish momentum ({r:.0f})")
            elif r >= 55:
                score += 0.5
                reasons.append(f"RSI bullish ({r:.0f})")
            elif r <= 30:
                score -= 1
                reasons.append(f"RSI strong bearish momentum ({r:.0f})")
            elif r <= 45:
                score -= 0.5
                reasons.append(f"RSI bearish ({r:.0f})")

        if vol_ratio[i] is not None and vol_ratio[i] >= 1.5:
            if score > 0:
                score += 0.5
                reasons.append(f"volume {vol_ratio[i]:.1f}x average confirms")
            elif score < 0:
                score -= 0.5
                reasons.append(f"volume {vol_ratio[i]:.1f}x average confirms")

        if score >= config.SIGNAL_BUY_SCORE:
            state = "buy"
        elif score <= config.SIGNAL_SELL_SCORE:
            state = "sell"
        else:
            state = "neutral"

        states[i] = state
        scores[i] = score
        reasons_by_bar[i] = reasons

    latest_idx = n - 1
    latest_state = states[latest_idx]

    # Find the most recent bar where the state differs from the one before
    # it -- this is what drives the "flash" (only fresh flips flash) and the
    # reasons shown for the current signal.
    changed_at = None
    for i in range(latest_idx, 0, -1):
        if states[i] != states[i - 1]:
            changed_at = candles[i]["t"]
            break
    else:
        if states[0] not in (None, "insufficient_data"):
            changed_at = candles[0]["t"]

    is_fresh = (
        changed_at is not None
        and (candles[latest_idx]["t"] - changed_at) <= (2 * config.CANDLE_BUCKET_SECONDS)
    )

    indicators = {
        "ma_fast": ma_fast, "ma_slow": ma_slow, "vwap": vwap, "rsi": rsi,
        "vol_ratio": vol_ratio, "state": states,
    }
    signal = {
        "state": latest_state or "insufficient_data",
        "score": round(scores[latest_idx], 2),
        "reasons": reasons_by_bar[latest_idx] or [],
        "changed_at": changed_at,
        "is_fresh": is_fresh,
    }
    return indicators, signal


def market_status():
    now_ny = datetime.datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return "closed"
    open_t = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    pre_t = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    post_t = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    if open_t <= now_ny <= close_t:
        return "open"
    if pre_t <= now_ny < open_t:
        return "pre-market"
    if close_t < now_ny <= post_t:
        return "after-hours"
    return "closed"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        api_key_configured=server_meta["api_key_configured"],
        candle_bucket_seconds=config.CANDLE_BUCKET_SECONDS,
    )


@app.route("/api/state")
def api_state():
    with state_lock:
        symbols_payload = []
        for sym, s in symbols_state.items():
            candles = list(s["candles"])
            if s["_current_candle"] is not None:
                cur = s["_current_candle"]
                candles = candles + [
                    {
                        "t": cur["bucket"] * config.CANDLE_BUCKET_SECONDS,
                        "o": cur["o"], "h": cur["h"], "l": cur["l"], "c": cur["c"], "vol": cur["vol"],
                    }
                ]
            indicators, signal = compute_signals(candles)
            symbols_payload.append(
                {
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "is_etf": s["is_etf"],
                    "price": s["price"],
                    "change": s["change"],
                    "percent": s["percent"],
                    "high": s["high"],
                    "low": s["low"],
                    "open": s["open"],
                    "candles": candles,
                    "indicators": indicators,
                    "signal": signal,
                    "last_update": s["last_update"],
                    "tick_source": s["last_tick_source"],
                    "error": s["error"],
                }
            )

    symbols_payload.sort(
        key=lambda x: abs(x["percent"]) if x["percent"] is not None else -1,
        reverse=True,
    )
    movers = [s["symbol"] for s in symbols_payload[: config.TOP_MOVERS_COUNT] if s["percent"] is not None]

    return jsonify(
        {
            "symbols": symbols_payload,
            "movers": movers,
            "meta": {
                "server_time": time.time(),
                "market_status": market_status(),
                "last_quote_cycle_finished": server_meta["last_quote_cycle_finished"],
                "websocket_status": server_meta["websocket_status"],
                "movers_screener_status": server_meta["movers_screener_status"],
                "api_key_configured": server_meta["api_key_configured"],
            },
        }
    )


@app.route("/api/track", methods=["POST"])
def api_track():
    payload = request.get_json(silent=True) or {}
    symbol = payload.get("symbol", "")
    result = ensure_symbol_tracked(symbol)
    return jsonify(result), (200 if result.get("ok") else 404)


@app.route("/api/news")
def api_news():
    with market_news_lock:
        items = list(market_news)
    return jsonify({"items": items, "status": server_meta["news_status"]})


if __name__ == "__main__":
    if not server_meta["api_key_configured"]:
        print(
            "\n*** No Finnhub API key configured yet. ***\n"
            "Get a free key at https://finnhub.io/register and set FINNHUB_API_KEY\n"
            "as an environment variable, or paste it into config.py.\n"
            "The server will start, but every quote/websocket call will fail until a key is set.\n"
        )

    # Must happen after screener_added_symbols/_at/_last_seen (defined further
    # up the module) exist, and before any thread starts touching them.
    _load_candle_cache()

    threading.Thread(target=poll_quotes_forever, daemon=True).start()
    threading.Thread(target=run_websocket_forever, daemon=True).start()
    threading.Thread(target=poll_movers_forever, daemon=True).start()
    threading.Thread(target=persist_candles_forever, daemon=True).start()
    threading.Thread(target=poll_market_news_forever, daemon=True).start()

    # threaded=True is important: without it, Flask's dev server handles one
    # request at a time. A single slow /api/track lookup (waiting on a rate
    # limit slot) would otherwise block every /api/state poll from the page
    # too, making the whole dashboard appear to freeze.
    try:
        app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
    finally:
        # Best-effort final save on Ctrl+C, so restarting to pick up the next
        # code change loses at most CANDLE_CACHE_SAVE_INTERVAL_SECONDS of
        # progress instead of whatever's happened since the last periodic save.
        _save_candle_cache()
