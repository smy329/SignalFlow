/* Sources page: live counters, the active/inactive switch and the per-source settings modal. */
(function () {
  window.LiveUI.every(3000, async function () {
    window.LiveUI.syncLive(await window.LiveUI.fetchDoc(window.location.href));
  });

  /* ---- active / inactive switch: flips without reloading the page ---- */
  function applyToggle(form, active) {
    const button = form.querySelector(".js-toggle");
    const knob = form.querySelector(".js-toggle-knob");
    button.setAttribute("aria-checked", active ? "true" : "false");
    button.title = active ? "Active: click to deactivate" : "Inactive: click to activate";
    button.classList.toggle("bg-emerald-500", active);
    button.classList.toggle("bg-red-500", !active);
    knob.classList.toggle("translate-x-6", active);
    knob.classList.toggle("translate-x-1", !active);
    const badge = form.closest("tr").querySelector("[data-status-badge]");
    badge.textContent = active ? "Active" : "Inactive";
    ["text-emerald-700", "bg-emerald-50"].forEach((c) => badge.classList.toggle(c, active));
    ["text-slate-500", "bg-slate-100"].forEach((c) => badge.classList.toggle(c, !active));
  }

  document.querySelectorAll(".js-toggle-form").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const button = form.querySelector(".js-toggle");
      button.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-Requested-With": "XMLHttpRequest" },
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error("toggle failed");
        applyToggle(form, (await response.json()).is_active);
      } catch (error) {
        form.submit(); // fall back to a normal page post
      } finally {
        button.disabled = false;
      }
    });
  });

  /* ---- settings modal ---- */
  const modal = document.getElementById("settings-modal");
  if (!modal) return;
  const form = document.getElementById("settings-form");
  const tpInputs = Array.from(form.querySelectorAll("input[data-tp]"));
  const slAfter = form.elements.sl_after_tp;
  const globals = JSON.parse(document.getElementById("world-globals").textContent);
  const LABELS = { NEAREST: "Nearest entry", FARTHEST: "Farthest entry", BOTH: "Both entries", TP1_TOUCH: "stop-loss at TP1 touch",
                   AFTER_FILLING_TP: "stop-loss after a chosen TP is filled", ONE_WAY: "One-Way Mode", IGNORE: "Ignore",
                   SIGNAL_DIRECTION: "According to signal's direction", OPPOSITE_DIRECTION: "Opposite to signal's direction" };
  const pretty = (v) => LABELS[v] || v;
  /* "Use global (currently ...)" hints, and the placeholders of the blank-means-global inputs */
  document.querySelectorAll("[data-global]").forEach(function (node) {
    const key = node.dataset.global;
    node.textContent = key === "tp_split" ? globals.tp_split.join(" / ") + "%" : pretty(String(globals[key]));
  });
  form.elements.leverage.placeholder = globals.leverage;
  form.elements.margin.placeholder = globals.margin || "none (rejects signals)";
  slAfter.placeholder = globals.sl_after_tp;
  const totalLine = document.getElementById("tp-total");
  const errorBox = document.getElementById("settings-errors");
  const saveButton = document.getElementById("settings-save");

  const checked = (name) => form.querySelector('input[name="' + name + '"]:checked');

  function readPercents() {
    return tpInputs.map(function (input) {
      const raw = input.value.trim();
      if (raw === "") return { value: 0, blank: true, ok: true };
      const ok = /^\d+$/.test(raw) && Number(raw) <= 100;
      return { value: ok ? Number(raw) : 0, blank: false, ok: ok };
    });
  }

  /* Mirrors trading/domain/strategy.py validate(); the server checks again on save. */
  function refresh() {
    const percents = readPercents();
    const errors = [];
    percents.forEach(function (p, i) {
      if (!p.ok) errors.push("TP" + (i + 1) + " must be a whole number between 0 and 100.");
      tpInputs[i].classList.toggle("text-red-600", !p.ok);
    });
    const total = percents.reduce((sum, p) => sum + p.value, 0);
    const custom = total > 0;
    totalLine.className = "text-xs mt-3 " + (total > 100 ? "text-red-600 font-medium" : "text-slate-500");
    if (total > 100) {
      totalLine.textContent = "Total " + total + "%: " + (total - 100) + "% over. The percentages must not add up to more than 100%.";
      errors.push("Take-profit percentages total " + total + "%, which is more than 100%.");
    } else if (!custom) {
      totalLine.textContent = "Nothing set: the global split is used (" + globals.tp_split.join(" / ") + "%).";
    } else if (total === 100) {
      totalLine.textContent = "Total 100%.";
    } else {
      totalLine.textContent = "Total " + total + "%. The remaining " + (100 - total) + "% is closed by the next target after the last one you set (or by that target if the signal has no more).";
    }

    const lev = form.elements.leverage.value.trim();
    if (lev !== "" && !(Number(lev) >= 1 && Number(lev) <= 2000)) errors.push("Leverage must be a number from 1 to 2000.");
    const mar = form.elements.margin.value.trim();
    if (mar !== "" && !(Number(mar) > 0)) errors.push("Margin per trade must be a number greater than 0.");

    const follow = (checked("follow_mode") && checked("follow_mode").value) || globals.follow_mode;
    const opposite = follow === "OPPOSITE_DIRECTION";  // one take-profit and a fixed stop-loss: those two strategies do not apply
    form.querySelectorAll("[data-follow-off]").forEach((node) => node.classList.toggle("opacity-40", opposite));
    document.getElementById("follow-note").classList.toggle("hidden", !opposite);

    const slMode = (checked("sl_mode") && checked("sl_mode").value) || globals.sl_mode;
    if (!opposite && slMode === "AFTER_FILLING_TP") {
      const n = slAfter.value.trim() === "" ? Number(globals.sl_after_tp) : Number(slAfter.value);
      if (!Number.isInteger(n) || n < 1 || n > 8) {
        errors.push("The stop-loss TP number must be a whole number from 1 to 8.");
      } else if (custom && percents[n - 1].value === 0) {
        errors.push("TP" + n + " closes nothing, so the stop-loss can never move after it.");
      }
    }

    errorBox.replaceChildren(...errors.map(function (text) {
      const line = document.createElement("span");
      line.className = "block";
      line.textContent = text;
      return line;
    }));
    saveButton.disabled = errors.length > 0;
  }

  function open(button) {
    form.action = button.dataset.action;
    document.getElementById("settings-title").textContent = button.dataset.name;
    form.elements.leverage.value = button.dataset.leverage;
    form.elements.margin.value = button.dataset.margin;
    form.elements.margin_type.value = button.dataset.marginType;
    const pick = (name, value) => (form.querySelector('input[name="' + name + '"][value="' + value + '"]:not(:disabled)')
      || form.querySelector('input[name="' + name + '"][value=""]')).checked = true;  /* unknown value: Use global */
    pick("follow_mode", button.dataset.followMode);
    pick("entry_mode", button.dataset.entryMode);
    tpInputs.forEach((input) => (input.value = ""));
    button.dataset.tp.split(",").forEach(function (value, i) {
      if (tpInputs[i]) tpInputs[i].value = value;
    });
    pick("sl_mode", button.dataset.slMode);
    slAfter.value = button.dataset.slAfterTp;
    pick("reversal_mode", button.dataset.reversalMode);
    refresh();
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    modal.setAttribute("aria-hidden", "false");
  }

  function close() {
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    modal.setAttribute("aria-hidden", "true");
  }

  document.querySelectorAll(".js-open-settings").forEach((button) => button.addEventListener("click", () => open(button)));
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", close));
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  form.addEventListener("submit", function (event) {
    refresh();
    if (saveButton.disabled) event.preventDefault();
  });
})();
