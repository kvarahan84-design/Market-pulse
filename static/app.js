(function () {
  "use strict";

  const POLL_MS = 4000;
  const grid = document.getElementById("ticker-grid");
  const emptyState = document.getElementById("empty-state");
  const lastUpdatedEl = document.getElementById("last-updated");
  const statusPill = document.getElementById("market-status");
  const searchBox = document.getElementById("search-box");
  const searchStatus = document.getElementById("search-status");
  const chips = document.querySelectorAll(".chip");
  const themeToggle = document.getElementById("theme-toggle");
  const bullishListEl = document.getElementById("bullish-list");
  const bullishEmptyEl = document.getElementById("bullish-empty");
  const bearishListEl = document.getElementById("bearish-list");
  const bearishEmptyEl = document.getElementById("bearish-empty");
  const sortSelect = document.getElementById("sort-select");
  const newsListEl = document.getElementById("news-list");
  const newsEmptyEl = document.getElementById("news-empty");

  // Comparators for the grid's sort order. "movers" (the default) ranks by
  // |% change| regardless of direction, matching what the server already
  // uses to pick the Top Movers set. "gainers"/"losers" rank by signed %
  // change so you can see who's actually up vs. down, biggest first.
  const SORT_COMPARATORS = {
    movers: (a, b) => Math.abs(b.percent ?? -1) - Math.abs(a.percent ?? -1),
    gainers: (a, b) => (b.percent ?? -Infinity) - (a.percent ?? -Infinity),
    losers: (a, b) => (a.percent ?? Infinity) - (b.percent ?? Infinity),
    alpha: (a, b) => a.symbol.localeCompare(b.symbol),
  };

  let currentFilter = "all";
  let searchTerms = []; // comma-separated search terms, OR-matched
  let latestState = null;
  const cardEls = new Map(); // symbol -> {root, canvas, ...}

  // Terms we've already tried to auto-track (successfully or not), so we don't
  // hammer /api/track on every keystroke/poll while an unresolved term sits
  // in the search box.
  const trackAttempted = new Set();
  const NON_TICKER_WORDS = new Set(["ETF", "STOCK"]); // synthetic tokens passesFilter() also matches on
  const TICKER_PATTERN = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  let trackDebounceTimer = null;

  // symbol -> the signal.changed_at timestamp we've already flashed, so a
  // fresh flip only flashes once (not on every ~4s poll while it's still
  // "fresh" server-side).
  const lastFlashedAt = new Map();

  // ---- remembering what you typed, across reloads --------------------------
  // Plain localStorage on this dashboard's own localhost origin -- this is a
  // real page in your own browser (not a sandboxed embed), so it persists
  // normally between visits.
  const LS_SEARCH_KEY = "marketpulse:searchQuery";
  const LS_FILTER_KEY = "marketpulse:filter";
  const LS_SORT_KEY = "marketpulse:sort";

  function lsGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null; // e.g. storage disabled -- degrade to non-persistent, don't break the page
    }
  }
  function lsSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* ignore */
    }
  }

  // ---- theme toggle -------------------------------------------------------
  themeToggle.addEventListener("click", () => {
    const html = document.documentElement;
    const current = html.getAttribute("data-theme");
    if (current === "dark") {
      html.setAttribute("data-theme", "light");
    } else if (current === "light") {
      html.removeAttribute("data-theme");
    } else {
      html.setAttribute("data-theme", "dark");
    }
    if (modalState.open) drawModalChart(); // repaint with new theme colors
  });

  // ---- filters --------------------------------------------------------------
  function selectFilter(filterName, persist) {
    currentFilter = filterName;
    chips.forEach((c) => c.classList.toggle("active", c.dataset.filter === filterName));
    if (persist) lsSet(LS_FILTER_KEY, filterName);
    renderGrid();
  }

  chips.forEach((chip) => {
    chip.addEventListener("click", () => selectFilter(chip.dataset.filter, true));
  });

  // ---- sorting ---------------------------------------------------------------
  let currentSort = "movers";

  function selectSort(sortName, persist) {
    if (!SORT_COMPARATORS[sortName]) return;
    currentSort = sortName;
    if (sortSelect.value !== sortName) sortSelect.value = sortName;
    if (persist) lsSet(LS_SORT_KEY, sortName);
    renderGrid();
  }

  sortSelect.addEventListener("change", () => selectSort(sortSelect.value, true));

  // Comma-separated search: "NVDA, ETF, coin" matches any card whose symbol or
  // name contains ANY of the comma-split terms (OR logic), not the raw string.
  // A term that looks like a ticker but isn't currently tracked gets fetched
  // live via /api/track, so you can search literally any symbol, not just
  // the ones in the starting watchlist.
  function applySearchValue(rawValue, persist) {
    searchTerms = rawValue
      .toUpperCase()
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    renderGrid();
    if (persist) lsSet(LS_SEARCH_KEY, rawValue);

    clearTimeout(trackDebounceTimer);
    trackDebounceTimer = setTimeout(trackUnknownTerms, 700);
  }

  searchBox.addEventListener("input", () => applySearchValue(searchBox.value, true));

  function knownSymbols() {
    const set = new Set();
    if (latestState) latestState.symbols.forEach((s) => set.add(s.symbol));
    return set;
  }

  function setSearchStatus(text, kind) {
    if (!text) {
      searchStatus.classList.add("hidden");
      searchStatus.textContent = "";
      return;
    }
    searchStatus.textContent = text;
    searchStatus.className = "small" + (kind ? " is-" + kind : "");
  }

  async function trackUnknownTerms() {
    const known = knownSymbols();
    const candidates = searchTerms.filter(
      (t) =>
        TICKER_PATTERN.test(t) &&
        !NON_TICKER_WORDS.has(t) &&
        !known.has(t) &&
        !trackAttempted.has(t)
    );
    if (candidates.length === 0) return;
    candidates.forEach((s) => trackAttempted.add(s));

    // Fire lookups in parallel (the server now has a dedicated, small rate
    // budget just for these so they don't queue up behind the background
    // poll loop) and show one combined status once they've all settled,
    // rather than a per-symbol message that the next symbol immediately
    // overwrites.
    setSearchStatus(`Looking up ${candidates.join(", ")}…`, "loading");

    const results = await Promise.all(
      candidates.map(async (symbol) => {
        try {
          const resp = await fetch("/api/track", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol }),
          });
          const data = await resp.json();
          return { symbol, ...data };
        } catch (err) {
          return { symbol, ok: false, error: "couldn't reach the server" };
        }
      })
    );

    const succeeded = results.filter((r) => r.ok).map((r) => r.symbol);
    const failed = results.filter((r) => !r.ok);

    let msg = "";
    if (succeeded.length) msg = `Tracking ${succeeded.join(", ")} — appearing shortly.`;
    if (failed.length) {
      msg += (msg ? " " : "") + failed.map((f) => `${f.symbol}: ${f.error || "not found"}`).join(" · ");
    }
    setSearchStatus(msg, failed.length ? "error" : "");
    if (!failed.length) setTimeout(() => setSearchStatus("", ""), 4000);
  }

  // ---- restore search box + filter chip from the last visit ----------------
  (function restoreSearchAndFilter() {
    const savedSearch = lsGet(LS_SEARCH_KEY);
    if (savedSearch) {
      searchBox.value = savedSearch;
      applySearchValue(savedSearch, false); // will re-track any dynamic tickers you'd typed before
    }
    const savedFilter = lsGet(LS_FILTER_KEY);
    if (savedFilter && document.querySelector(`.chip[data-filter="${savedFilter}"]`)) {
      selectFilter(savedFilter, false);
    }
    const savedSort = lsGet(LS_SORT_KEY);
    if (savedSort) selectSort(savedSort, false);
  })();

  function getColor(varName) {
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  }

  function formatMoney(v) {
    if (v === null || v === undefined) return "—";
    return "$" + v.toFixed(v >= 1000 ? 0 : 2);
  }

  function formatPercent(v) {
    if (v === null || v === undefined) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(2)}%`;
  }

  function timeAgo(unixSeconds) {
    if (!unixSeconds) return "—";
    const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    return `${Math.floor(diff / 3600)}h ago`;
  }

  function formatClock(unixSeconds) {
    if (!unixSeconds) return "—";
    const d = new Date(unixSeconds * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // ---- candlestick chart ----------------------------------------------------
  // candles: [{t, o, h, l, c, vol}, ...] oldest -> newest, one per 2-min bucket.
  // indicators (optional): {ma_fast, ma_slow, vwap, state}, each array the
  // same length as candles, aligned index-for-index -- drawn as overlay lines
  // (MA/VWAP) and, per opts.markers, the literal word BUY/SELL stamped
  // directly on the chart at the bar(s) where indicators.state flips:
  //   opts.markers === "latest" -> only the most recent flip (small cards)
  //   opts.markers === "all"    -> every flip visible in this window (modal)
  // Returns the layout (or null) so callers (the modal) can hit-test the mouse
  // position against candles for a crosshair/tooltip.
  function drawCandles(canvas, candles, overallDirection, indicators, opts) {
    opts = opts || {};
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width, 60);
    const h = Math.max(rect.height, 30);
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (!candles || candles.length < 2) {
      ctx.fillStyle = getColor("--text-muted");
      ctx.font = "11px system-ui";
      ctx.fillText("gathering live candles…", 6, h / 2 + 3);
      return null;
    }

    const good = getColor("--good");
    const critical = getColor("--critical");
    const neutral = getColor("--series-1");
    const maFastColor = getColor("--series-2");
    const maSlowColor = getColor("--series-3");
    const vwapColor = getColor("--series-4");

    const highs = candles.map((c) => c.h);
    const lows = candles.map((c) => c.l);
    let min = Math.min(...lows);
    let max = Math.max(...highs);
    // Widen the range to fit overlay lines that can sit outside the candle
    // wicks (VWAP especially can drift beyond the visible price range).
    if (indicators) {
      [indicators.ma_fast, indicators.ma_slow, indicators.vwap].forEach((series) => {
        (series || []).forEach((v) => {
          if (v == null) return;
          if (v < min) min = v;
          if (v > max) max = v;
        });
      });
    }
    const range = max - min || 1;
    const padTop = 6;
    const padBottom = 6;

    const n = candles.length;
    const slot = w / n;
    const bodyWidth = Math.max(2, Math.min(12, slot * 0.6));
    const gap = Math.max(1, slot * 0.15);

    const yFor = (price) => h - padBottom - ((price - min) / range) * (h - padTop - padBottom);
    const xFor = (i) => i * slot + slot / 2;

    candles.forEach((c, i) => {
      const cx = xFor(i);
      const color = c.c > c.o ? good : c.c < c.o ? critical : neutral;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;

      ctx.beginPath();
      ctx.lineWidth = 1;
      ctx.moveTo(cx, yFor(c.h));
      ctx.lineTo(cx, yFor(c.l));
      ctx.stroke();

      const yOpen = yFor(c.o);
      const yClose = yFor(c.c);
      const top = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.5, Math.abs(yClose - yOpen));
      ctx.fillRect(cx - bodyWidth / 2 + gap / 2, top, Math.max(1, bodyWidth - gap), bodyH);
    });

    function drawLine(series, color) {
      if (!series) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      let started = false;
      series.forEach((v, i) => {
        if (v == null) return;
        const x = xFor(i);
        const y = yFor(v);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      if (started) ctx.stroke();
    }
    if (indicators) {
      drawLine(indicators.vwap, vwapColor);
      drawLine(indicators.ma_slow, maSlowColor);
      drawLine(indicators.ma_fast, maFastColor);
    }

    function flipIndices(states) {
      const idxs = [];
      for (let i = 1; i < states.length; i++) {
        if (states[i] === states[i - 1]) continue;
        if (states[i] !== "buy" && states[i] !== "sell") continue;
        idxs.push(i);
      }
      return idxs;
    }

    function stampSignal(i, type) {
      const cx = xFor(i);
      const isBuy = type === "buy";
      const color = isBuy ? good : critical;
      const markerY = isBuy
        ? Math.min(h - 4, yFor(candles[i].l) + 9)
        : Math.max(4, yFor(candles[i].h) - 9);

      ctx.fillStyle = color;
      ctx.beginPath();
      if (isBuy) {
        ctx.moveTo(cx, markerY + 5);
        ctx.lineTo(cx - 5, markerY - 5);
        ctx.lineTo(cx + 5, markerY - 5);
      } else {
        ctx.moveTo(cx, markerY - 5);
        ctx.lineTo(cx - 5, markerY + 5);
        ctx.lineTo(cx + 5, markerY + 5);
      }
      ctx.closePath();
      ctx.fill();

      // The literal word, stamped right on the chart -- not just a shape or
      // a badge elsewhere on the page.
      const fontSize = opts.labelFontSize || 9;
      ctx.font = `bold ${fontSize}px system-ui`;
      ctx.textAlign = "center";
      const labelY = isBuy
        ? Math.min(h - 2, markerY + fontSize + 3)
        : Math.max(fontSize, markerY - fontSize - 1);
      // A light background behind the text keeps it legible over candles/lines.
      const label = type.toUpperCase();
      const textWidth = ctx.measureText(label).width;
      ctx.fillStyle = getColor("--surface-1");
      ctx.globalAlpha = 0.85;
      ctx.fillRect(cx - textWidth / 2 - 2, labelY - fontSize, textWidth + 4, fontSize + 3);
      ctx.globalAlpha = 1;
      ctx.fillStyle = color;
      ctx.fillText(label, cx, labelY);
      ctx.textAlign = "left";
    }

    if (indicators && indicators.state && opts.markers) {
      const flips = flipIndices(indicators.state);
      if (opts.markers === "all") {
        flips.forEach((i) => stampSignal(i, indicators.state[i]));
      } else if (opts.markers === "latest" && flips.length) {
        const lastFlip = flips[flips.length - 1];
        const lastState = indicators.state[indicators.state.length - 1];
        // Only stamp if that flip is still the CURRENT state (not stale --
        // e.g. it flipped to buy, then back to neutral since).
        if (indicators.state[lastFlip] === lastState) {
          stampSignal(lastFlip, lastState);
        }
      }
    }

    const lastClose = candles[candles.length - 1].c;
    const lineColor = overallDirection > 0 ? good : overallDirection < 0 ? critical : neutral;
    ctx.strokeStyle = lineColor;
    ctx.setLineDash([2, 2]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, yFor(lastClose));
    ctx.lineTo(w, yFor(lastClose));
    ctx.stroke();
    ctx.setLineDash([]);

    return { w, h, min, max, range, slot, padTop, padBottom, yFor, candles };
  }

  // ---- ticker cards -----------------------------------------------------

  function buildCard(sym) {
    const root = document.createElement("div");
    root.className = "ticker-card";
    root.tabIndex = 0;
    root.setAttribute("role", "button");
    root.setAttribute("aria-label", `Open larger chart for ${sym.symbol}`);
    root.innerHTML = `
      <div class="card-top">
        <div>
          <span class="card-symbol"></span>
          ${sym.is_etf ? '<span class="etf-badge">ETF</span>' : ""}
          <span class="card-signal hidden"></span>
          <span class="card-name"></span>
        </div>
      </div>
      <div class="card-price-row">
        <span class="card-price"></span>
        <span class="card-delta"></span>
      </div>
      <canvas class="card-chart"></canvas>
      <div class="card-meta">
        <span class="card-updated"></span>
        <span class="card-range"></span>
      </div>
      <div class="card-error hidden"></div>
    `;
    grid.appendChild(root);
    const els = {
      root,
      symbol: root.querySelector(".card-symbol"),
      name: root.querySelector(".card-name"),
      price: root.querySelector(".card-price"),
      delta: root.querySelector(".card-delta"),
      canvas: root.querySelector(".card-chart"),
      updated: root.querySelector(".card-updated"),
      range: root.querySelector(".card-range"),
      error: root.querySelector(".card-error"),
      signal: root.querySelector(".card-signal"),
    };
    cardEls.set(sym.symbol, els);

    const open = () => openModal(sym.symbol);
    root.addEventListener("click", open);
    root.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });

    return els;
  }

  function passesFilter(sym, moversSet) {
    if (searchTerms.length > 0) {
      const hay = (sym.symbol + " " + sym.name + (sym.is_etf ? " ETF" : " STOCK")).toUpperCase();
      const matchesAny = searchTerms.some((term) => hay.includes(term));
      if (!matchesAny) return false;
    }
    switch (currentFilter) {
      case "movers":
        return moversSet.has(sym.symbol);
      case "etf":
        return sym.is_etf;
      case "stocks":
        return !sym.is_etf;
      case "gainers":
        return sym.percent !== null && sym.percent > 0;
      case "losers":
        return sym.percent !== null && sym.percent < 0;
      default:
        return true;
    }
  }

  function renderGrid() {
    if (!latestState) return;
    const moversSet = new Set(latestState.movers || []);
    let visibleCount = 0;
    let modalSymData = null;

    // Re-sort every poll, not just on first paint -- otherwise cards stay
    // frozen in whatever order they first appeared in, and the grid never
    // visibly reflects who's actually up or down as prices change.
    const comparator = SORT_COMPARATORS[currentSort] || SORT_COMPARATORS.movers;
    const sorted = [...latestState.symbols].sort(comparator);

    sorted.forEach((sym) => {
      let els = cardEls.get(sym.symbol);
      if (!els) els = buildCard(sym);
      grid.appendChild(els.root); // re-appending an existing node moves it -- keeps DOM order in sync with the sort

      if (sym.symbol === modalState.symbol) modalSymData = sym;

      const visible = passesFilter(sym, moversSet);
      els.root.classList.toggle("hidden", !visible);
      if (!visible) return;
      visibleCount++;

      els.root.classList.toggle("is-mover", moversSet.has(sym.symbol));
      els.symbol.textContent = sym.symbol;
      els.name.textContent = sym.name;
      els.price.textContent = formatMoney(sym.price);

      const dir = sym.percent === null ? 0 : sym.percent > 0 ? 1 : sym.percent < 0 ? -1 : 0;
      els.delta.textContent =
        sym.percent === null
          ? "—"
          : `${dir > 0 ? "▲" : dir < 0 ? "▼" : "•"} ${formatPercent(sym.percent)}`;
      els.delta.className = "card-delta " + (dir > 0 ? "up" : dir < 0 ? "down" : "flat");

      const sig = sym.signal || { state: "insufficient_data" };
      if (sig.state === "buy" || sig.state === "sell") {
        els.signal.textContent = sig.state.toUpperCase();
        els.signal.className = "card-signal sig-chip sig-chip-" + sig.state;
        els.signal.title = (sig.reasons || []).join(" · ");
        els.signal.classList.remove("hidden");
      } else {
        els.signal.classList.add("hidden");
      }

      // Flash the card border once per fresh flip -- keyed on changed_at so
      // the same flip never replays the animation on later polls.
      if (sig.is_fresh && (sig.state === "buy" || sig.state === "sell")) {
        if (lastFlashedAt.get(sym.symbol) !== sig.changed_at) {
          lastFlashedAt.set(sym.symbol, sig.changed_at);
          els.root.classList.remove("flash-buy", "flash-sell");
          void els.root.offsetWidth; // force reflow so the animation restarts
          els.root.classList.add(sig.state === "buy" ? "flash-buy" : "flash-sell");
          setTimeout(() => els.root.classList.remove("flash-buy", "flash-sell"), 4000);
        }
      }

      drawCandles(els.canvas, sym.candles, dir, sym.indicators, { markers: "latest", labelFontSize: 8 });

      const liveTag = sym.tick_source === "websocket" ? "⚡" : "";
      els.updated.textContent = sym.last_update ? `${liveTag} updated ${timeAgo(sym.last_update)}` : "waiting…";
      els.range.textContent =
        sym.low && sym.high ? `${sym.low.toFixed(2)}–${sym.high.toFixed(2)}` : "";

      if (sym.error) {
        els.error.textContent = "quote error: " + sym.error;
        els.error.classList.remove("hidden");
      } else {
        els.error.classList.add("hidden");
      }
    });

    emptyState.classList.toggle("hidden", visibleCount !== 0);

    if (modalState.open && modalSymData) updateModal(modalSymData);

    renderSignalLists();
  }

  // ---- "Bullish now" / "Bearish now" side-panel lists ----------------------
  // A ranked view of the whole tracked universe's current signal state, so
  // "what should I even look at today" doesn't require scanning every card.
  // Independent of the grid's search box / filter chips on purpose -- this is
  // meant to be a fixed reference regardless of what the grid is showing.

  function renderSignalLists() {
    if (!latestState) return;

    const bullish = [];
    const bearish = [];
    let warmingUpCount = 0;
    latestState.symbols.forEach((sym) => {
      const sig = sym.signal;
      if (!sig) return;
      if (sig.state === "buy") bullish.push(sym);
      else if (sig.state === "sell") bearish.push(sym);
      else if (sig.state === "insufficient_data") warmingUpCount++;
    });

    // Strongest conviction first (score is more positive for buy, more
    // negative for sell -- see compute_signals() in app.py).
    bullish.sort((a, b) => (b.signal.score || 0) - (a.signal.score || 0));
    bearish.sort((a, b) => (a.signal.score || 0) - (b.signal.score || 0));

    const total = latestState.symbols.length;
    // Signals need SIGNAL_MA_SLOW_PERIOD bars of live candles before they can
    // fire at all -- candle history lives only in memory, so every restart
    // (and every newly-tracked symbol) starts this warm-up over from zero.
    // If most of the tracked universe is still warming up, say so instead of
    // just showing an empty list that looks like "nothing is happening".
    const warmupNote =
      warmingUpCount > 0 && warmingUpCount >= total * 0.5
        ? `Still building live candle history since the last restart (${warmingUpCount}/${total} symbols not ready yet) -- signals need a few minutes of data before they can fire. Check back shortly.`
        : null;

    renderSignalList(bullishListEl, bullishEmptyEl, bullish, "No BUY signals right now.", warmupNote);
    renderSignalList(bearishListEl, bearishEmptyEl, bearish, "No SELL signals right now.", warmupNote);
  }

  function renderSignalList(container, emptyEl, symbols, emptyText, warmupNote) {
    emptyEl.classList.toggle("hidden", symbols.length > 0);
    emptyEl.textContent = symbols.length > 0 ? "" : warmupNote || emptyText;
    container.innerHTML = "";
    symbols.forEach((sym) => {
      const dir = sym.percent === null ? 0 : sym.percent > 0 ? 1 : sym.percent < 0 ? -1 : 0;
      const row = document.createElement("button");
      row.type = "button";
      row.className = "signal-list-row";
      row.title = (sym.signal.reasons || []).join(" · ") || "no reasons recorded";
      row.innerHTML =
        `<span class="signal-list-symbol">${sym.symbol}</span>` +
        `<span class="signal-list-price">${formatMoney(sym.price)}</span>` +
        `<span class="signal-list-delta ${dir > 0 ? "up" : dir < 0 ? "down" : "flat"}">` +
        `${sym.percent === null ? "—" : formatPercent(sym.percent)}</span>`;
      row.addEventListener("click", () => openModal(sym.symbol));
      container.appendChild(row);
    });
  }

  function updateStatusPill(status) {
    statusPill.className = "status-pill status-" + status;
    const labels = {
      open: "market open",
      closed: "market closed",
      "pre-market": "pre-market",
      "after-hours": "after-hours",
    };
    statusPill.textContent = labels[status] || status;
  }

  // ---- chart modal --------------------------------------------------------

  const modalEls = {
    root: document.getElementById("chart-modal"),
    backdrop: document.getElementById("chart-modal-backdrop"),
    close: document.getElementById("modal-close"),
    symbol: document.getElementById("modal-symbol"),
    etfBadge: document.getElementById("modal-etf-badge"),
    name: document.getElementById("modal-name"),
    price: document.getElementById("modal-price"),
    delta: document.getElementById("modal-delta"),
    canvas: document.getElementById("modal-canvas"),
    tooltip: document.getElementById("modal-tooltip"),
  };

  const modalState = { open: false, symbol: null, layout: null, direction: 0 };

  function openModal(symbol) {
    const symData = (latestState && latestState.symbols.find((s) => s.symbol === symbol)) || null;
    if (!symData) return;
    modalState.open = true;
    modalState.symbol = symbol;
    modalEls.root.classList.remove("hidden");
    updateModal(symData);
    modalEls.close.focus();
  }

  function closeModal() {
    modalState.open = false;
    modalState.symbol = null;
    modalEls.root.classList.add("hidden");
    modalEls.tooltip.classList.add("hidden");
  }

  modalEls.close.addEventListener("click", closeModal);
  modalEls.backdrop.addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modalState.open) closeModal();
  });

  let modalLastSymData = null;
  const modalSignalEl = document.getElementById("modal-signal");

  function updateModal(symData) {
    modalLastSymData = symData;
    modalEls.symbol.textContent = symData.symbol;
    modalEls.etfBadge.classList.toggle("hidden", !symData.is_etf);
    modalEls.name.textContent = symData.name;
    modalEls.price.textContent = formatMoney(symData.price);

    const dir = symData.percent === null ? 0 : symData.percent > 0 ? 1 : symData.percent < 0 ? -1 : 0;
    modalState.direction = dir;
    modalEls.delta.textContent =
      symData.percent === null ? "—" : `${dir > 0 ? "▲" : dir < 0 ? "▼" : "•"} ${formatPercent(symData.percent)}`;
    modalEls.delta.className = "card-delta " + (dir > 0 ? "up" : dir < 0 ? "down" : "flat");

    if (modalSignalEl) {
      const sig = symData.signal || { state: "insufficient_data", reasons: [] };
      const reasonsText = (sig.reasons || []).join(" · ");
      if (sig.state === "buy" || sig.state === "sell") {
        modalSignalEl.innerHTML =
          `<span class="sig-chip sig-chip-${sig.state}">${sig.state.toUpperCase()}</span> ` +
          `<span class="muted small">${reasonsText}</span>`;
      } else if (sig.state === "insufficient_data") {
        modalSignalEl.innerHTML = '<span class="muted small">Gathering enough history to compute signals…</span>';
      } else {
        modalSignalEl.innerHTML =
          `<span class="muted small">Neutral${reasonsText ? " — " + reasonsText : ""}</span>`;
      }
    }

    drawModalChart();
  }

  function drawModalChart() {
    if (!modalLastSymData) return;
    modalState.layout = drawCandles(
      modalEls.canvas,
      modalLastSymData.candles,
      modalState.direction,
      modalLastSymData.indicators,
      { markers: "all", labelFontSize: 10 }
    );
  }

  modalEls.canvas.addEventListener("mousemove", (e) => {
    const layout = modalState.layout;
    if (!layout) return;
    const rect = modalEls.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    let idx = Math.floor(x / layout.slot);
    idx = Math.max(0, Math.min(layout.candles.length - 1, idx));
    const c = layout.candles[idx];
    if (!c) return;

    const tt = modalEls.tooltip;
    tt.innerHTML = "";
    const addRow = (label, value) => {
      const row = document.createElement("div");
      const lbl = document.createElement("span");
      lbl.textContent = label + ": ";
      const val = document.createElement("span");
      val.className = "tt-value";
      val.textContent = value;
      row.appendChild(lbl);
      row.appendChild(val);
      tt.appendChild(row);
    };
    addRow("Time", formatClock(c.t));
    addRow("Open", "$" + c.o.toFixed(2));
    addRow("High", "$" + c.h.toFixed(2));
    addRow("Low", "$" + c.l.toFixed(2));
    addRow("Close", "$" + c.c.toFixed(2));

    tt.classList.remove("hidden");
    const cx = idx * layout.slot + layout.slot / 2;
    let left = cx + 14;
    if (left + 150 > layout.w) left = cx - 164;
    tt.style.left = Math.max(4, left) + "px";
    tt.style.top = "8px";
  });
  modalEls.canvas.addEventListener("mouseleave", () => {
    modalEls.tooltip.classList.add("hidden");
  });

  window.addEventListener("resize", () => {
    if (latestState) renderGrid();
    if (modalState.open) drawModalChart();
  });

  // ---- market news ------------------------------------------------------
  // Polled on its own, much slower cadence (headlines refresh every 5 min
  // server-side anyway) so it doesn't ride on the 4s price-poll cycle.

  const NEWS_POLL_MS = 60000;

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Simple relevance check: does this headline mention a currently-tracked
  // symbol's ticker (whole word) or company name? Used only to highlight,
  // never to filter -- this is a general market feed, not per-symbol.
  function newsMentionsTracked(title) {
    if (!latestState) return false;
    return latestState.symbols.some((sym) => {
      const tickerHit = new RegExp(`\\b${sym.symbol}\\b`).test(title);
      const nameHit = sym.name && sym.name.length >= 4 && title.toLowerCase().includes(sym.name.toLowerCase());
      return tickerHit || nameHit;
    });
  }

  function renderNews(items) {
    newsEmptyEl.classList.toggle("hidden", items.length > 0);
    newsListEl.innerHTML = items
      .map((it) => {
        const relevant = newsMentionsTracked(it.title);
        return (
          `<a class="news-row${relevant ? " news-row-relevant" : ""}" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">` +
          `<span class="news-title">${escapeHtml(it.title)}</span>` +
          `</a>`
        );
      })
      .join("");
  }

  async function pollNews() {
    try {
      const resp = await fetch("/api/news");
      const data = await resp.json();
      renderNews(data.items || []);
    } catch (err) {
      // silent -- news is a nice-to-have side panel, not core functionality
    } finally {
      setTimeout(pollNews, NEWS_POLL_MS);
    }
  }

  // ---- polling --------------------------------------------------------------

  async function poll() {
    try {
      const resp = await fetch("/api/state");
      const data = await resp.json();
      latestState = data;
      renderGrid();
      updateStatusPill(data.meta.market_status);
      const wsNote = data.meta.websocket_status ? ` · feed: ${data.meta.websocket_status}` : "";
      lastUpdatedEl.textContent = "server updated " + timeAgo(data.meta.last_quote_cycle_finished) + wsNote;
    } catch (err) {
      lastUpdatedEl.textContent = "connection error — retrying…";
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  poll();
  pollNews();
})();
