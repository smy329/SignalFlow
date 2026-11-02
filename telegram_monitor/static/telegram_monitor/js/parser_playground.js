/* Parser Playground: POSTs the pasted text to the dry-run endpoint and shows the verdict. */
(function () {
  const url = JSON.parse(document.getElementById("pg-config").textContent).url;
  const csrf = document.querySelector("#pg-csrf input[name=csrfmiddlewaretoken]").value;
  const input = document.getElementById("pg-input");
  const button = document.getElementById("pg-parse");
  const box = document.getElementById("pg-result");
  const statusEl = document.getElementById("pg-status");
  const reasonEl = document.getElementById("pg-reason");
  const details = document.getElementById("pg-details");
  const fields = document.getElementById("pg-fields");
  const errorEl = document.getElementById("pg-error");

  const BADGE = {
    Signal: "text-emerald-700 bg-emerald-50",
    "Not a signal": "text-slate-600 bg-slate-100",
  };

  function row(label, value) {
    const dt = document.createElement("dt");
    dt.className = "text-slate-400";
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.className = "font-medium";
    dd.textContent = value;
    fields.append(dt, dd);
  }

  function show(result) {
    errorEl.classList.add("hidden");
    box.classList.remove("hidden");
    statusEl.textContent = result.status;
    statusEl.className = "inline-flex px-2.5 py-0.5 rounded-full text-xs font-semibold " + (BADGE[result.status] || BADGE["Not a signal"]);
    reasonEl.textContent = result.reason;
    reasonEl.className = "font-medium " + (result.is_signal ? "text-emerald-700" : "text-slate-700");

    fields.replaceChildren();
    const s = result.signal;
    details.classList.toggle("hidden", !s);
    if (!s) return;
    row("Symbol", s.symbol);
    row("Direction", s.direction === "BUY" ? "LONG / BUY" : "SHORT / SELL");
    row("Entry", s.entry);
    row("Targets", s.targets.map((t, i) => "TP" + (i + 1) + " " + t).join("   "));
    row("Stop loss", s.stoploss);
    row("Leverage", s.leverage + "x");
  }

  function fail(message) {
    box.classList.add("hidden");
    errorEl.textContent = message;
    errorEl.classList.remove("hidden");
  }

  async function parse() {
    button.disabled = true;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        credentials: "same-origin",
        body: JSON.stringify({ text: input.value }),
      });
      const data = await response.json();
      if (!response.ok) return fail(data.detail || "Could not parse that message.");
      show(data);
    } catch (e) {
      fail("Could not reach the server.");
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener("click", parse);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) parse(); });
  document.getElementById("pg-clear").addEventListener("click", function () {
    input.value = "";
    box.classList.add("hidden");
    errorEl.classList.add("hidden");
    input.focus();
  });
})();
