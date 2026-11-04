/* Trades page: one table per tab/URL (Positions, Pending or Cancelled), polled in the background.
   Pending rows also have a Trade Now button (enter at the current market price). Positions and Pending have the same Close button per row. On a position it closes the trade at market now (the
   trade then shows in History); on a pending trade it cancels the unfilled entry orders (the trade then shows in
   Cancelled with the reason "Closed manually"). */
(function () {
  const UI = window.TradingUI;
  const c = UI.col;
  const cfg = document.getElementById("trade-config").dataset;
  // Manual trades come from no channel, but they are Real or Forward: show that instead.
  const cols = (list) => (cfg.manual ? [c.mode, ...list.filter((x) => x !== c.channel)] : list);

  const csrfInput = document.querySelector("#trade-csrf input[name=csrfmiddlewaretoken]");
  const errorBox = document.getElementById("close-error");
  const closing = new Set();  // trade ids being closed: their button stays disabled across the 5-second refreshes
  let table = null;

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.classList.toggle("hidden", !message);
  }

  // Real money asks first when closing; the server enforces the same rule. Forward closes with one click.
  const isReal = (row) => (cfg.manual ? row.mode === "Real" : cfg.scope === "real");

  // One request per row at a time: Trade Now and Close share the "busy" state of the row.
  async function act(row, url, body, failure) {
    const id = row.trade_id;
    showError("");
    closing.add(id);
    table.refresh();  // redraw the buttons as disabled
    try {
      const response = await fetch(url.replace("/0/", "/" + id + "/"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfInput.value, Accept: "application/json" },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await response.json(); } catch (e) { /* not JSON: handled below */ }
      if (!response.ok || !data.ok) showError(data.message || failure + " (server error " + response.status + ").");
    } catch (e) {
      showError("Could not reach the server. Check Pending, Positions and History before trying again.");
    } finally {
      closing.delete(id);
      table.refresh();
    }
  }

  function closeTrade(row) {
    const label = row.symbol + " " + row.side;
    const pending = cfg.tab === "pending";
    if (isReal(row) && !window.confirm(pending ? "Close " + label + "? Its pending entry orders will be cancelled." : "Close " + label + " at market?")) return;
    return act(row, cfg.closeUrl, { confirmed: true }, "Could not close " + label);
  }

  // No confirmation, also for Real: the click is the decision (the server checks price and size).
  function tradeNow(row) {
    return act(row, cfg.tradeNowUrl, {}, "Could not trade " + row.symbol + " " + row.side);
  }

  const BTN = "text-xs font-semibold px-2.5 py-1 rounded border disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap ";

  function closeButton(r, busy) {
    const btn = UI.el("button", BTN + "border-red-200 text-red-700 bg-red-50 hover:bg-red-100", busy ? "Closing..." : "Close");
    btn.type = "button";
    btn.disabled = busy;
    btn.title = cfg.tab === "pending" ? "Cancel this pending trade's entry orders now" : "Close this position at the market price now";
    btn.addEventListener("click", () => closeTrade(r));
    return btn;
  }

  const closeCol = {
    label: "",
    weight: 8,
    cell: (r) => {
      // No trade record (a position opened by hand on Binance): nothing to close from here.
      if (r.trade_id === null || r.trade_id === undefined) return UI.el("span", "text-slate-300", "—");
      return closeButton(r, closing.has(r.trade_id));
    },
  };

  // Pending rows: Trade Now first, Close second.
  const pendingActionsCol = {
    label: "",
    weight: 16,
    cell: (r) => {
      if (r.trade_id === null || r.trade_id === undefined) return UI.el("span", "text-slate-300", "—");
      const busy = closing.has(r.trade_id);
      const box = UI.el("div", "flex flex-wrap gap-1.5");
      const now = UI.el("button", BTN + "border-emerald-200 text-emerald-700 bg-emerald-50 hover:bg-emerald-100", busy ? "Working..." : "Trade Now");
      now.type = "button";
      now.disabled = busy;
      now.title = "Enter at the current market price now (a second entry order stays pending)";
      now.addEventListener("click", () => tradeNow(r));
      box.appendChild(now);
      box.appendChild(closeButton(r, busy));
      return box;
    },
  };

  const TABS = {
    positions: { interval: 5000, columns: cols([c.channel, c.side, c.symbol, c.entry, c.margin, c.leverage, c.sl, c.tps, c.livePnl, c.pct, c.opened, closeCol]) },
    pending: { interval: 5000, columns: cols([c.channel, c.side, c.symbol, c.current, c.entry, c.margin, c.leverage, c.sl, c.tps, pendingActionsCol]) },
    cancelled: { interval: 5000, columns: cols([c.channel, c.side, c.symbol, c.state, c.entry, c.margin, c.leverage, c.sl, c.tps, c.reason, c.cancelledAt]) },
  };
  const tab = TABS[cfg.tab];

  table = UI.mountTable({
    url: cfg.url, head: "table-head", body: "table-body", empty: "table-empty", count: "row-count",
    interval: tab.interval, columns: tab.columns,
  });
  table.start();

  // The bracket count on every tab: the active one is set by its table above; the other two are read
  // from their own list endpoints, so a trade that moved tabs is visible without switching.
  const otherCounts = Array.from(document.querySelectorAll("[data-count-url]:not([data-count-active])"));
  async function refreshCounts() {
    await Promise.all(otherCounts.map(async (node) => {
      try {
        const response = await fetch(node.dataset.countUrl, { headers: { Accept: "application/json" } });
        const data = await response.json();
        node.textContent = "(" + (data.rows || []).length + ")";
      } catch (e) { /* keep the last known number */ }
    }));
  }
  const tickCounts = () => { if (!document.hidden) refreshCounts(); };
  document.addEventListener("visibilitychange", tickCounts);
  refreshCounts();
  setInterval(tickCounts, 5000);
})();
