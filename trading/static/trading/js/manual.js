/* Manual Trade page. The signal box is the main thing; the options are remembered between visits and are
   replaced by the Settings values whenever the "Manual trading" settings page is saved (config.version changes).
   Everything that comes from the server goes through textContent, never innerHTML. */
(function () {
  const config = JSON.parse(document.getElementById("manual-config").textContent);
  const urls = document.getElementById("manual-urls").dataset;
  const csrf = document.querySelector("#manual-csrf input[name=csrfmiddlewaretoken]").value;
  const STORE = "manualTrade.options";
  const LAST_TP = "manualTrade.lastSlTp";
  const $ = (id) => document.getElementById(id);
  const NAMES = ["leverage", "margin", "margin_type", "entry_mode", "sl_mode", "sl_after_tp", "reversal_mode", "tp_split"];
  const inputs = {};
  NAMES.forEach((n) => (inputs[n] = $("opt-" + n)));
  const text = $("signal-text");
  const placeBtn = $("place-btn");
  let busy = false;
  let pollTimer = null;
  let analyzeTimer = null;
  let analyzeSeq = 0;
  let slOptions = null;  // the stop-loss options on screen (null = the panel is hidden)

  function el(tag, cls, content) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  /* ---- options: defaults, remembered values, summary ---- */

  Object.keys(config.choices).forEach((name) => {
    config.choices[name].forEach((choice) => inputs[name].appendChild(new Option(choice, choice)));
  });

  function readOptions() {
    const out = {};
    NAMES.forEach((n) => (out[n] = inputs[n].value));
    return out;
  }

  function writeOptions(values) {
    NAMES.forEach((n) => {
      if (values[n] === undefined || values[n] === null) return;
      inputs[n].value = (n === "sl_mode" && values[n] === "AFTER_TP") ? "AFTER_FILLING_TP" : values[n];  /* value remembered before the rename */
    });
  }

  function remember() {
    try { localStorage.setItem(STORE, JSON.stringify({ version: config.version, values: readOptions() })); } catch (e) { /* private mode: just not remembered */ }
  }

  function summary() {
    const o = readOptions();
    const sl = o.sl_mode + (o.sl_mode === "AFTER_FILLING_TP" ? " " + o.sl_after_tp : "");
    $("options-summary").textContent = [o.leverage + "x", o.margin + " USDT", o.margin_type, o.entry_mode, "SL " + sl, o.reversal_mode, "TP " + o.tp_split.replace(/\s*,\s*/g, "/")].join(" · ");
  }

  function load() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) { saved = null; }
    writeOptions(config.defaults);
    if (saved && saved.version === config.version && saved.values) writeOptions(saved.values);  // else: Settings changed, start from them
    remember();
    summary();
  }

  NAMES.forEach((n) => {
    inputs[n].addEventListener("input", () => { clearFieldError(n); remember(); summary(); });
    inputs[n].addEventListener("change", () => { remember(); summary(); });
  });

  $("defaults-btn").addEventListener("click", () => {
    writeOptions(config.defaults);
    clearErrors();
    remember();
    summary();
  });

  /* ---- errors ---- */

  function clearFieldError(name) {
    const node = $("err-" + name);
    if (node) { node.textContent = ""; node.classList.add("hidden"); }
  }

  function clearErrors() {
    NAMES.forEach(clearFieldError);
    [$("text-error"), $("options-error")].forEach((n) => { n.textContent = ""; n.classList.add("hidden"); });
  }

  function showErrors(data) {
    clearErrors();
    const fields = data.fields || {};
    let optionError = false;
    Object.keys(fields).forEach((name) => {
      if (name === "text") return;
      const node = $("err-" + name);
      if (node) { node.textContent = fields[name]; node.classList.remove("hidden"); optionError = true; }
    });
    if (fields.text) {
      $("text-error").textContent = fields.text;
      $("text-error").classList.remove("hidden");
    } else if (!optionError) {
      $("text-error").textContent = data.message || "Something went wrong.";
      $("text-error").classList.remove("hidden");
    } else {
      $("options-error").textContent = data.message || "";
      $("options-error").classList.toggle("hidden", !data.message);
      $("options").open = true;
    }
  }

  /* ---- server calls ---- */

  async function post(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(payload),
    });
    let data;
    try { data = await response.json(); } catch (e) { data = { ok: false, message: "The server sent an unreadable reply (HTTP " + response.status + ")." }; }
    return data;
  }

  const mode = () => document.querySelector("input[name=mode]:checked").value;
  const payload = (extra) => Object.assign({ text: text.value, mode: mode(), options: readOptions() }, extra || {});

  /* ---- result panel ---- */

  function resultBox(kind) {
    const box = $("result");
    box.className = "mt-4 rounded-lg border px-4 py-3 text-sm " + {
      info: "border-slate-200 bg-slate-50 text-slate-700",
      ok: "border-emerald-200 bg-emerald-50 text-emerald-800",
      bad: "border-red-200 bg-red-50 text-red-700",
      wait: "border-amber-200 bg-amber-50 text-amber-800",
    }[kind];
    box.replaceChildren();
    return box;
  }

  function humanize(code) {
    if (!code) return "";
    const t = code.toLowerCase().replace(/_/g, " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function showOutcome(d) {
    if (d.state === "waiting" || d.state === "placing") {
      resultBox("info").appendChild(el("span", "", "Placing the trade..."));
      return false;
    }
    if (d.state === "rejected" || d.state === "failed" || d.state === "cancelled") {
      const box = resultBox("bad");
      const title = { failed: "Failed", cancelled: "Cancelled", rejected: "Not placed" }[d.state] || "Not placed";
      box.appendChild(el("div", "font-semibold", title + " · " + d.mode_label + " · " + d.symbol + " " + d.side));
      box.appendChild(el("div", "mt-1", d.message || humanize(d.code)));
      return true;
    }
    const box = resultBox("ok");
    box.appendChild(el("div", "font-semibold", "Placed · " + d.mode_label + " · " + d.symbol + " " + d.side + " · " + d.label));
    const bits = [];
    if (d.entries && d.entries.length) bits.push("Entry " + d.entries.join(" / "));
    if (d.stoploss) bits.push("SL " + d.stoploss);
    if (d.targets && d.targets.length) bits.push("TP " + d.targets.join(", "));
    if (d.quantity) bits.push("Qty " + d.quantity);
    if (bits.length) box.appendChild(el("div", "mt-1 text-xs", bits.join(" · ")));
    const link = el("a", "inline-block mt-2 underline font-medium", "View in Manual trades");
    link.href = d.url;
    box.appendChild(link);
    text.value = "";  // placed: ready for the next signal
    showSlOptions(null);
    text.focus();
    return true;
  }

  function poll(signalId) {
    clearTimeout(pollTimer);
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const response = await fetch(urls.result.replace(/0\/$/, signalId + "/"), { headers: { Accept: "application/json" } });
        if (showOutcome(await response.json())) return;
      } catch (e) { /* keep trying */ }
      if (tries >= 45) {
        const box = resultBox("wait");
        box.appendChild(el("span", "", "The engine has not answered yet. Check Manual > Trades before sending the signal again."));
        return;
      }
      pollTimer = setTimeout(tick, 1000);
    };
    tick();
  }

  /* ---- placing ---- */

  function setBusy(value) {
    busy = value;
    placeBtn.disabled = value;
    $("sl-options").querySelectorAll("button").forEach((b) => { b.disabled = value || b.dataset.usable !== "1"; });
  }

  async function submit(extra) {
    const data = await post(urls.submit, payload(extra));
    if (!data.ok) { setBusy(false); $("result").classList.add("hidden"); showErrors(data); return false; }
    resultBox("info").appendChild(el("span", "", "Placing the trade..."));
    poll(data.signal_id);
    setBusy(false);
    return true;
  }

  function confirmRows(p) {
    const rows = [
      ["Trade in", p.mode_label], ["Symbol", p.symbol], ["Direction", p.side],
      ["Entries in the signal", p.entries.join(" / ")], ["Entries to trade (" + p.entry_mode + ")", p.entries_used.join(" / ")],
      ["Stop-loss", p.stoploss], ["Targets", p.targets.join(", ") || "—"],
      ["Leverage", p.leverage], ["Margin", p.margin + " (" + p.margin_type + ")"],
      ["Stop-loss mode", p.sl_mode], ["Take-profit split", p.tp_split], ["Reversal", p.reversal_mode],
    ];
    const body = $("confirm-body");
    body.replaceChildren();
    rows.forEach((r) => { body.appendChild(el("dt", "text-slate-500", r[0])); body.appendChild(el("dd", "font-medium break-all", r[1])); });
  }

  function closeModal() { $("confirm-modal").classList.add("hidden"); setBusy(false); text.focus(); }

  async function place() {
    if (busy) return;
    clearErrors();
    $("result").classList.add("hidden");
    if (!text.value.trim()) { showErrors({ fields: { text: "Paste a signal first." } }); return; }
    if (slOptions) { showErrors({ fields: { text: "This signal has no stop-loss. Click one of the TP buttons below." } }); return; }
    setBusy(true);
    try {
      if (mode() === "real") {
        const data = await post(urls.preview, payload());
        if (!data.ok) { setBusy(false); showErrors(data); return; }
        confirmRows(data.preview);
        $("confirm-modal").classList.remove("hidden");
        $("confirm-ok").focus();
        return;  // busy stays on until the modal is answered
      }
      await submit();
    } catch (e) {
      setBusy(false);
      showErrors({ message: "Could not reach the server." });
    }
  }

  $("confirm-ok").addEventListener("click", async () => {
    $("confirm-modal").classList.add("hidden");
    try { await submit({ confirmed: true }); } catch (e) { setBusy(false); showErrors({ message: "Could not reach the server." }); }
  });
  $("confirm-cancel").addEventListener("click", closeModal);
  $("confirm-modal").addEventListener("click", (e) => { if (e.target === $("confirm-modal")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("confirm-modal").classList.contains("hidden")) closeModal(); });

  placeBtn.addEventListener("click", place);
  text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); place(); } });
  text.addEventListener("input", () => { $("text-error").classList.add("hidden"); });
  $("clear-btn").addEventListener("click", () => { text.value = ""; showSlOptions(null); clearErrors(); $("result").classList.add("hidden"); text.focus(); });

  /* ---- signals without a stop-loss: one click sets SL 1:1 with a TP and places the trade ---- */

  function lastTp() {
    try { return parseInt(localStorage.getItem(LAST_TP) || "", 10) || 0; } catch (e) { return 0; }
  }

  function modeLabel() { return mode() === "real" ? "Real" : "Forward"; }

  function showSlOptions(data, takeFocus) {
    slOptions = data;
    const panel = $("sl-panel");
    const box = $("sl-options");
    box.replaceChildren();
    if (!data) { panel.classList.add("hidden"); return; }
    const last = lastTp();
    $("sl-title").textContent = data.symbol + " " + data.side + ".";
    $("sl-entry").textContent = "Measured from the nearest entry, " + data.entry + ".";
    $("sl-mode-label").textContent = modeLabel();
    data.options.forEach((o) => {
      const pct = +Number(o.percent).toFixed(2);
      const btn = el("button", "text-left rounded-lg border px-3 py-2 text-sm leading-tight " + (o.usable
        ? "border-slate-300 bg-white hover:bg-slate-900 hover:text-white focus:outline-none focus:ring-2 focus:ring-slate-500"
        : "border-slate-200 bg-slate-100 text-slate-400 cursor-not-allowed"));
      btn.type = "button";
      btn.dataset.tp = o.tp;
      btn.dataset.usable = o.usable ? "1" : "0";
      btn.disabled = !o.usable;
      if (o.usable && o.tp === last) btn.classList.add("ring-2", "ring-slate-900");
      const head = el("div", "flex items-center justify-between");
      head.appendChild(el("span", "font-semibold", "TP" + o.tp));
      if (o.tp <= 9) head.appendChild(el("kbd", "text-[10px] rounded border border-current px-1 opacity-60", String(o.tp)));
      btn.appendChild(head);
      if (o.usable) {
        btn.appendChild(el("div", "", "SL " + o.sl_price + " (−" + pct + "%)"));
        btn.appendChild(el("div", "text-[11px] opacity-60", "TP " + o.tp_price + " (+" + pct + "%)" + (o.tp === last ? " · last used" : "")));
      } else {
        btn.appendChild(el("div", "text-[11px]", "TP " + o.tp_price));
        btn.appendChild(el("div", "text-[11px]", o.reason));
      }
      btn.addEventListener("click", () => placeWithSl(o.tp));
      box.appendChild(btn);
    });
    panel.classList.remove("hidden");
    if (takeFocus) {         // after a paste: so the digit keys pick a TP instead of typing into the signal
      text.blur();
      panel.focus({ preventScroll: true });
    }
  }

  async function analyze(takeFocus) {
    const seq = ++analyzeSeq;
    let data;
    try { data = await post(urls.analyze, { text: text.value, options: readOptions() }); } catch (e) { return; }
    if (seq !== analyzeSeq) return;  // the text changed while waiting
    if (!data.ok) { showSlOptions(null); showErrors(data); return; }
    clearErrors();
    showSlOptions(data.needs_stoploss ? data : null, takeFocus);
  }

  function scheduleAnalyze(delay, takeFocus) {
    clearTimeout(analyzeTimer);
    analyzeSeq += 1;       // an answer for older text is ignored
    showSlOptions(null);   // never leave buttons on screen for text that has changed
    if (!text.value.trim()) return;
    analyzeTimer = setTimeout(() => analyze(takeFocus), delay);
  }

  async function placeWithSl(tp) {
    if (busy || !slOptions) return;
    clearErrors();
    $("result").classList.add("hidden");
    setBusy(true);
    try { localStorage.setItem(LAST_TP, String(tp)); } catch (e) { /* not remembered */ }
    try {
      if (await submit({ sl_tp: tp })) showSlOptions(null);  // on a refusal the buttons stay for another try
    } catch (e) {
      setBusy(false);
      showErrors({ message: "Could not reach the server." });
    }
  }

  text.addEventListener("input", (e) => e.inputType === "insertFromPaste" ? scheduleAnalyze(0, true) : scheduleAnalyze(400, false));
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => { if (slOptions) $("sl-mode-label").textContent = modeLabel(); }));
  document.addEventListener("keydown", (e) => {
    if (!slOptions || busy || e.ctrlKey || e.metaKey || e.altKey || !/^[1-9]$/.test(e.key)) return;
    const tag = (e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const btn = $("sl-options").querySelector('button[data-tp="' + e.key + '"]');
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  });

  load();
})();
