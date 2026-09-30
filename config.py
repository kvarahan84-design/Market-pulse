"""
Configuration for the real-time market dashboard.

Setup:
  1. Get a free API key at https://finnhub.io/register
  2. Set it as an environment variable (never paste it into this file):
       Windows:   setx FINNHUB_API_KEY "your_key_here"   (then open a new terminal)
       Mac/Linux: export FINNHUB_API_KEY=your_key_here
  3. Edit WATCHLIST below to add or remove tickers.
"""

import os

# --- API key -----------------------------------------------------------------
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

# --- Display -----------------------------------------------------------------
TOP_MOVERS_COUNT = 8          # symbols shown in "Top movers", ranked by |% change|
HISTORY_MAX_POINTS = 180      # candles kept per symbol (180 x 2 min = 6 hours)

# --- Candle cache (survives quick restarts) ----------------------------------
# Saves watchlist candles to candle_cache.json so a restart doesn't reset the
# ~24-min signal warmup. Cache older than the max age is discarded.
PERSIST_CANDLES = True
CANDLE_CACHE_SAVE_INTERVAL_SECONDS = 30
CANDLE_CACHE_MAX_AGE_SECONDS = 900  # 15 min

# --- Market news (public WSJ / Dow Jones RSS headlines) ----------------------
USE_MARKET_NEWS = True
NEWS_RSS_FEEDS = [
    "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
]
NEWS_POLL_SECONDS = 300
NEWS_MAX_ITEMS = 25

# --- Finnhub rate limits (free tier cap is 60 calls/min; keep the sum below) --
MAX_CALLS_PER_MINUTE = 45     # background quote polling
TRACK_CALLS_PER_MINUTE = 10   # on-demand ticker lookups from the search box

# Real-time candles from Finnhub's websocket. Set False if your network blocks
# websockets; the app falls back to coarser candles built from REST polling.
USE_WEBSOCKET_CANDLES = True

# --- Buy/sell signal engine --------------------------------------------------
# Rule-based heuristic (EMAs, VWAP, RSI, volume), not ML and not backtested.
# See compute_signals() in app.py for the scoring rules.
CANDLE_BUCKET_SECONDS = 120   # 2-minute candles (charts and signals)

SIGNAL_MA_FAST_PERIOD = 5     # bars (10 min)
SIGNAL_MA_SLOW_PERIOD = 12    # bars (24 min); sets the signal warmup time
SIGNAL_RSI_PERIOD = 7         # bars (14 min)
SIGNAL_VOLUME_AVG_PERIOD = 5  # bars

# Score needed to fire. Higher magnitude = fewer, higher-conviction signals.
SIGNAL_BUY_SCORE = 2.0
SIGNAL_SELL_SCORE = -2.0

# --- Movers screener (unofficial Yahoo Finance via yahooquery, no key) -------
# Adds today's gainers/losers/most-active symbols to the tracked list.
# Unofficial and may break; set False to use WATCHLIST only.
USE_MOVERS_SCREENER = True
MOVERS_SCREENER_POLL_SECONDS = 90
MOVERS_SCREENER_COUNT = 25          # symbols requested per category
MOVERS_MAX_DYNAMIC_SYMBOLS = 20     # max screener-added symbols at once
MOVERS_MIN_TRACK_SECONDS = 1800     # eviction backstop for symbols that never warm up

# --- Watchlist ---------------------------------------------------------------
# Always tracked. Keep ETFs tagged is_etf=True so they're labeled correctly.
WATCHLIST = [
    # Broad market ETFs
    {"symbol": "SPY", "name": "S&P 500 ETF", "is_etf": True},
    {"symbol": "QQQ", "name": "Nasdaq 100 ETF", "is_etf": True},
    {"symbol": "IWM", "name": "Russell 2000 ETF", "is_etf": True},
    {"symbol": "DIA", "name": "Dow Jones ETF", "is_etf": True},
    {"symbol": "VTI", "name": "Total Market ETF", "is_etf": True},

    # Sector / theme ETFs
    {"symbol": "XLF", "name": "Financials ETF", "is_etf": True},
    {"symbol": "XLE", "name": "Energy ETF", "is_etf": True},
    {"symbol": "XLK", "name": "Technology ETF", "is_etf": True},
    {"symbol": "SMH", "name": "Semiconductor ETF", "is_etf": True},
    {"symbol": "ARKK", "name": "ARK Innovation ETF", "is_etf": True},
    {"symbol": "TLT", "name": "20+ Yr Treasury ETF", "is_etf": True},
    {"symbol": "GLD", "name": "Gold ETF", "is_etf": True},
    {"symbol": "VXX", "name": "VIX Short-Term ETF", "is_etf": True},

    # Mega-cap
    {"symbol": "AAPL", "name": "Apple", "is_etf": False},
    {"symbol": "MSFT", "name": "Microsoft", "is_etf": False},
    {"symbol": "NVDA", "name": "NVIDIA", "is_etf": False},
    {"symbol": "AMZN", "name": "Amazon", "is_etf": False},
    {"symbol": "GOOGL", "name": "Alphabet", "is_etf": False},
    {"symbol": "META", "name": "Meta Platforms", "is_etf": False},
    {"symbol": "TSLA", "name": "Tesla", "is_etf": False},
    {"symbol": "AVGO", "name": "Broadcom", "is_etf": False},
    {"symbol": "AMD", "name": "Advanced Micro Devices", "is_etf": False},
    {"symbol": "NFLX", "name": "Netflix", "is_etf": False},

    # Semis / memory
    {"symbol": "MU", "name": "Micron Technology", "is_etf": False},
    {"symbol": "SNDK", "name": "SanDisk", "is_etf": False},

    # High-beta
    {"symbol": "PLTR", "name": "Palantir", "is_etf": False},
    {"symbol": "SMCI", "name": "Super Micro Computer", "is_etf": False},
    {"symbol": "COIN", "name": "Coinbase", "is_etf": False},
    {"symbol": "MSTR", "name": "MicroStrategy", "is_etf": False},
    {"symbol": "SOFI", "name": "SoFi Technologies", "is_etf": False},
    {"symbol": "MARA", "name": "Marathon Digital", "is_etf": False},
    {"symbol": "RIOT", "name": "Riot Platforms", "is_etf": False},
    {"symbol": "RIVN", "name": "Rivian", "is_etf": False},
    {"symbol": "LCID", "name": "Lucid Group", "is_etf": False},
    {"symbol": "NIO", "name": "NIO Inc", "is_etf": False},
    {"symbol": "HOOD", "name": "Robinhood", "is_etf": False},
    {"symbol": "DKNG", "name": "DraftKings", "is_etf": False},
]
