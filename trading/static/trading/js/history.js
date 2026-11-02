/* History page: closed trades, refreshed every 5s. */
(function () {
  const UI = window.TradingUI;
  const c = UI.col;
  const cfg = document.getElementById("trade-config").dataset;
  const columns = [c.channel, c.side, c.symbol, c.entry, c.margin, c.leverage, c.closing, c.result, c.pnl, c.pct, c.opened, c.closed];
  UI.mountTable({
    url: cfg.historyUrl,
    head: "history-head", body: "history-body", empty: "history-empty", interval: 5000,
    columns: cfg.manual ? [c.mode, ...columns.filter((x) => x !== c.channel)] : columns,
  }).start();
})();
