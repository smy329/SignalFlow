/* Shared helpers and the generic polling table used by the Trades and History pages.
   Everything that comes from Telegram (channel names) goes through textContent, never innerHTML. */
window.TradingUI = (function () {
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function fmt(n, digits) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("en-US", { maximumSignificantDigits: digits || 8 });
  }

  function money(n, places) {
    if (n === null || n === undefined) return "—";
    const v = Number(n);
    return (v > 0 ? "+" : "") + v.toFixed(places === undefined ? 2 : places);
  }

  function pnlClass(n) {
    if (n === null || n === undefined || Number(n) === 0) return "text-slate-700";
    return Number(n) > 0 ? "text-emerald-600" : "text-red-600";
  }

  function time(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  const REASONS = {
    PRICE_HIT_TP1: "Price hit TP1 before entry",
    PRICE_HIT_SL: "Price hit SL before entry",
    PRICE_HIT_TP1_AND_SL: "Price hit TP1 and SL before entry",
    ENTRY_CANCELED: "Entry cancelled on the exchange",
    ENTRY_EXPIRED: "Entry order expired",
    CLOSED_MANUALLY: "Closed manually",
    REVERSED: "Closed by reversal signal",
  };

  function humanize(code) {
    if (!code) return "—";
    if (REASONS[code]) return REASONS[code];
    const text = code.toLowerCase().replace(/_/g, " ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  /* ---- cell builders: each returns a DOM node ---- */

  function sideBadge(side) {
    const long = side === "LONG";
    return el("span", "text-xs font-bold px-2 py-0.5 rounded whitespace-nowrap " + (long ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"), side);
  }

  function targets(list) {
    const wrap = el("div", "flex flex-wrap gap-1");
    (list || []).forEach(function (t, i) {
      wrap.appendChild(el("span", "text-xs px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200", "TP" + (i + 1) + " " + fmt(t)));
    });
    if (!list || !list.length) wrap.appendChild(el("span", "text-slate-400", "—"));
    return wrap;
  }

  function colored(text, value) {
    return el("span", "font-medium " + pnlClass(value), text);
  }

  const plain = (text) => el("span", "", text);

  function modeBadge(mode) {
    const real = mode === "Real";
    return el("span", "text-xs font-semibold px-2 py-0.5 rounded whitespace-nowrap " + (real ? "bg-amber-50 text-amber-700" : "bg-sky-50 text-sky-700"), mode || "—");
  }

  /* Column = { label, cell(row) -> Node }.  Shared by every table. */
  const col = {
    mode: { label: "Mode", weight: 8, cell: (r) => modeBadge(r.mode) },
    channel: {
      label: "Channel", weight: 13,
      cell: (r) => {
        const box = el("span", "", r.channel || "—");
        if (r.opposite) {  // traded as the mirror image of the signal
          const tag = el("span", "ml-1.5 rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-violet-700 whitespace-nowrap", "Opposite");
          tag.title = "Traded opposite to the signal: reverse direction, the signal's SL as take-profit and its TP1 as stop-loss";
          box.appendChild(tag);
        }
        return box;
      },
    },
    side: { label: "Side", weight: 8, cell: (r) => sideBadge(r.side) },
    symbol: { label: "Symbol", weight: 11, cell: (r) => el("span", "font-semibold", r.symbol) },
    entry: {
      label: "Entry Price",
      weight: 10,
      cell: (r) => plain(r.entry_prices && r.entry_prices.length > 1 ? r.entry_prices.map((p) => fmt(p)).join(" / ") : fmt(r.entry_price)),
    },
    current: { label: "Current Price", weight: 10, cell: (r) => plain(fmt(r.current_price)) },
    margin: { label: "Margin", weight: 10, cell: (r) => plain(r.margin === null || r.margin === undefined ? "—" : "$" + fmt(r.margin, 6)) },
    leverage: { label: "Leverage", weight: 9, cell: (r) => plain(r.leverage ? r.leverage + "x" : "—") },
    sl: { label: "SL", weight: 9, cell: (r) => el("span", "text-red-600", fmt(r.stoploss)) },
    tps: { label: "TPs", weight: 15, cell: (r) => targets(r.targets) },
    livePnl: { label: "Live PnL (USDT)", weight: 11, cell: (r) => colored(money(r.pnl, 4), r.pnl) },
    pnl: { label: "PnL (USDT)", weight: 10, cell: (r) => colored(money(r.pnl, 4), r.pnl) },
    pct: { label: "% of margin", weight: 10, cell: (r) => colored(r.pnl_pct === null || r.pnl_pct === undefined ? "—" : money(r.pnl_pct, 2) + "%", r.pnl_pct) },
    closing: { label: "Closing Price", weight: 10, cell: (r) => plain(fmt(r.exit_price)) },
    result: { label: "Result", weight: 12, cell: (r) => colored(humanize(r.result), r.pnl) },
    reason: { label: "Reason", weight: 17, cell: (r) => el("span", "text-slate-700", humanize(r.result)) },
    opened: { label: "Opened at", weight: 11, cell: (r) => el("span", "", time(r.opened_at)) },
    closed: { label: "Closed at", weight: 11, cell: (r) => el("span", "", time(r.closed_at)) },
    cancelledAt: { label: "Cancelled at", weight: 11, cell: (r) => el("span", "", time(r.closed_at)) },
  };

  /* Polling table. `refs`: head/body/empty (+ optional count/note elements). Returns {start, stop}. */
  function mountTable(opts) {
    const head = document.getElementById(opts.head);
    const body = document.getElementById(opts.body);
    const empty = document.getElementById(opts.empty);
    const count = opts.count ? document.getElementById(opts.count) : null;
    const note = document.getElementById("note");
    let timer = null;
    let busy = false;

    const tr = el("tr");
    const totalWeight = opts.columns.reduce((sum, c) => sum + c.weight, 0);
    opts.columns.forEach((c) => {
      const th = el("th", "px-2 py-2 text-left font-medium align-bottom", c.label);
      th.style.width = ((c.weight / totalWeight) * 100).toFixed(2) + "%";
      tr.appendChild(th);
    });
    head.appendChild(tr);

    function render(rows) {
      body.replaceChildren();
      rows.forEach(function (row) {
        const line = el("tr");
        opts.columns.forEach((c) => {
          const td = el("td", "px-2 py-2.5");
          td.appendChild(c.cell(row));
          line.appendChild(td);
        });
        body.appendChild(line);
      });
      empty.classList.toggle("hidden", rows.length > 0);
      if (count) count.textContent = rows.length ? "(" + rows.length + ")" : "";
    }

    function setNote(message) {
      if (!note) return;
      note.textContent = message || "";
      note.classList.toggle("hidden", !message);
    }

    async function refresh() {
      if (busy) return;
      busy = true;
      try {
        const response = await fetch(opts.url, { headers: { Accept: "application/json" } });
        const data = await response.json();
        render(data.rows || []);
        setNote(data.message);
      } catch (e) {
        setNote("Could not reach the server, retrying...");
      } finally {
        busy = false;
      }
    }

    function tick() { if (!document.hidden) refresh(); }
    document.addEventListener("visibilitychange", tick);  // catch up the moment the tab is shown again

    return {
      refresh: refresh,
      start: function () { refresh(); timer = setInterval(tick, opts.interval || 5000); },
      stop: function () { clearInterval(timer); timer = null; },
    };
  }

  return { el: el, fmt: fmt, money: money, time: time, col: col, mountTable: mountTable };
})();
