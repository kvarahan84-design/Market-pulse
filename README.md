# Market Pulse: Real-Time Movers Dashboard

A local web dashboard for day traders. Every watched ticker (stocks and ETFs) is shown at once, each with a live 2-minute candlestick chart and a rule-based BUY/SELL signal. It also auto-discovers today's biggest market movers and shows live market headlines.

I built it as a personal trading tool after missing a large intraday move: one screen showing what's moving, why, and what the indicators say, without clicking into each ticker.

<!-- Add a screenshot: save it as docs/screenshot.png and uncomment the line below -->
<!-- ![Dashboard screenshot](docs/screenshot.png) -->

## Features

- **Live candlestick grid:** 2-minute OHLCV candles built from Finnhub's real-time trades websocket, with cards auto-ranked by absolute % change.
- **Signal engine:** scores each candle using EMA crossover, VWAP, RSI, and volume, and flags BUY or SELL when enough indicators agree. Every signal shows the reasons behind it.
- **Market-wide movers:** periodically pulls the day's gainers, losers, and most-active symbols and adds them to the tracked list, evicting the coldest picks when full.
- **Search any ticker:** type any symbol to validate it against Finnhub and add it live.
- **Chart detail view:** click any card for a larger interactive chart with a crosshair, OHLC tooltip, MA/VWAP overlays, and signal markers.
- **Market news panel:** public WSJ/Dow Jones RSS headlines, highlighting any that mention a tracked ticker.
- **Restart-safe:** candle history is cached to disk, so restarting doesn't reset the signal warmup.
- **Light and dark themes.**

## Disclaimer

This project is provided for educational and informational purposes only. It is not financial, investment, or trading advice, and nothing it displays is a recommendation to buy or sell any security.

The software is provided "as is," without warranty of any kind. Market data, signals, and news may be delayed, incomplete, or inaccurate. Third-party data sources (Finnhub, Yahoo Finance, WSJ/Dow Jones RSS) may change or stop working without notice.

Trading involves substantial risk of loss. You are solely responsible for your own trading decisions. The author accepts no liability for any losses, damages, or other consequences arising from use of this software.

Users are responsible for complying with the terms of service of any data provider they use, including Finnhub's API terms.

## Architecture

A small Python (Flask) backend runs on your machine and serves the page. The backend is needed to hold a persistent websocket connection, pace API calls under free-tier rate limits, and cache data, which a static HTML page can't do safely with an API key.

- **Backend:** Python, Flask, and background threads for the websocket feed, REST quote polling, the movers screener, and news
- **Data:** Finnhub (websocket trades plus REST quotes), Yahoo Finance screener via `yahooquery`, and WSJ RSS feeds
- **Frontend:** HTML, CSS, and JavaScript, polling the local API every few seconds

## Setup

**1. Get a free Finnhub API key** at https://finnhub.io/register

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

On Windows, use `py -m pip install -r requirements.txt` if `pip` isn't recognized.

**3. Set your API key as an environment variable** (never put it in the code)

```bash
setx FINNHUB_API_KEY "your_key_here"      # Windows (then open a new terminal)
export FINNHUB_API_KEY=your_key_here      # Mac/Linux
```

**4. Run it**

```bash
python app.py
```

Then open **http://127.0.0.1:5050** in your browser.

## Signal Engine

A transparent, rule-based scoring system, not a trained ML model and not a backtested strategy. `compute_signals()` in `app.py` combines four indicators, each adding a small positive or negative score:

- **Trend:** fast EMA versus slow EMA (5 and 12 bars by default, or 10 and 24 minutes)
- **Value:** price versus VWAP, computed over the candle history in memory (an approximation of session VWAP)
- **Momentum:** RSI (7 bars by default), read as trend-confirming rather than overbought/oversold
- **Confirmation:** current volume versus its recent average (websocket data only)

A BUY or SELL fires when the combined score crosses `SIGNAL_BUY_SCORE` or `SIGNAL_SELL_SCORE` (±2.0 by default). All periods and thresholds are tunable in `config.py`.

**This is not financial advice. Day trading carries real risk of loss.**

## Configuration

All settings live in `config.py`: the watchlist, candle size, signal periods and thresholds, screener limits, news feeds, rate limits, and cache behavior. Each setting has a one-line comment.

## Limitations

- **The movers screener uses Yahoo Finance's unofficial endpoint.** It has no SLA and could break without notice. Set `USE_MOVERS_SCREENER = False` to fall back to the watchlist only. Alpaca's official movers endpoint or a paid Polygon/Finnhub screener could replace it without changing anything downstream.
- **Some networks block websockets.** Set `USE_WEBSOCKET_CANDLES = False` to fall back to coarser REST-built candles, which have no volume data.
- **News is headlines and links only.** Paywalled WSJ articles still require your own subscription.
- **Signals need about 24 minutes of warmup** after a cold start before they can fire.

## Tech Notes

Built with Python and Flask, developed with AI assistance (Claude) and directed, tested, and used daily by me for live trading.
