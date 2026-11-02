/**
 * Realtime signal dashboard: renders the server-provided initial signals,
 * then opens a WebSocket to /ws/signals/ for live updates. No polling.
 */
(function () {
    const grid = document.getElementById("signal-grid");
    const emptyState = document.getElementById("empty-state");
    const signalCountEl = document.getElementById("signal-count");
    const wsDot = document.getElementById("ws-dot");
    const wsLabel = document.getElementById("ws-label");

    const seenIds = new Set();
    let signalCount = parseInt(signalCountEl.textContent, 10) || 0;

    function setStatus(state) {
        const styles = {
            connecting: ["bg-amber-400", "Connecting..."],
            connected: ["bg-emerald-500", "Live"],
            disconnected: ["bg-red-500", "Reconnecting..."],
        };
        const [dotClass, label] = styles[state];
        wsDot.className = `h-2.5 w-2.5 rounded-full ${dotClass}`;
        wsLabel.textContent = label;
    }

    function timeAgo(isoString) {
        const seconds = Math.max(0, Math.floor((Date.now() - new Date(isoString).getTime()) / 1000));
        if (seconds < 60) return `${seconds}s ago`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        return `${Math.floor(seconds / 3600)}h ago`;
    }

    function directionBadge(direction) {
        const isBuy = direction === "BUY";
        const label = isBuy ? "LONG / BUY" : "SHORT / SELL";
        const arrow = isBuy ? "\u25B2" : "\u25BC";
        const classes = isBuy
            ? "bg-emerald-50 text-emerald-700 border-emerald-200"
            : "bg-red-50 text-red-700 border-red-200";
        return `<span class="inline-flex items-center gap-1 text-xs font-semibold px-2 py-1 rounded-full border ${classes}">${arrow} ${label}</span>`;
    }

    function targetChips(targets) {
        return targets
            .map(
                (t, i) => `
            <div class="bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 text-center">
                <div class="text-[10px] uppercase tracking-wide text-slate-400 font-medium">TP${i + 1}</div>
                <div class="text-sm font-semibold text-slate-800">${t}</div>
            </div>`
            )
            .join("");
    }

    function renderCard(signal, { animate = false } = {}) {
        const card = document.createElement("div");
        card.dataset.id = signal.id;
        card.className =
            "bg-white rounded-xl border border-slate-200 shadow-sm p-4 flex flex-col gap-3" +
            (animate ? " signal-card-new" : "");

        card.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-base font-bold tracking-tight">${signal.symbol}</span>
                ${directionBadge(signal.direction)}
            </div>

            <div class="grid grid-cols-2 gap-3 text-sm">
                <div>
                    <div class="text-[10px] uppercase tracking-wide text-slate-400 font-medium">Entry</div>
                    <div class="font-semibold text-slate-800">${signal.entry}</div>
                </div>
                <div>
                    <div class="text-[10px] uppercase tracking-wide text-slate-400 font-medium">Stop Loss</div>
                    <div class="font-semibold text-red-600">${signal.stoploss}</div>
                </div>
            </div>

            <div>
                <div class="text-[10px] uppercase tracking-wide text-slate-400 font-medium mb-1">Take Profit</div>
                <div class="grid grid-cols-3 gap-1.5">${targetChips(signal.targets)}</div>
            </div>

            <div class="flex items-center justify-between text-xs text-slate-500 border-t border-slate-100 pt-2.5">
                <div class="flex items-center gap-2">
                    <span class="bg-slate-100 px-2 py-0.5 rounded-full font-medium">${signal.leverage}x</span>
                    <span class="bg-slate-100 px-2 py-0.5 rounded-full font-medium">${signal.margin_type}</span>
                </div>
                <span>${signal.source && signal.source.name ? signal.source.name : signal.channel}</span>
            </div>
            <div class="text-[11px] text-slate-400 text-right" data-timestamp="${signal.created_at}">
                Received ${timeAgo(signal.created_at)}
            </div>
        `;
        return card;
    }

    function addSignal(signal, { animate = false, prepend = false } = {}) {
        if (seenIds.has(signal.id)) return; // guards against duplicate cards after WS reconnect
        seenIds.add(signal.id);
        emptyState.classList.add("hidden");
        const card = renderCard(signal, { animate });
        if (prepend) {
            grid.prepend(card);
        } else {
            grid.appendChild(card);
        }
    }

    function updateCount(delta) {
        signalCount += delta;
        signalCountEl.textContent = signalCount;
    }

    // ---- initial render from server-side bootstrap data ----
    const bootstrap = JSON.parse(document.getElementById("bootstrap-signals").textContent || "[]");
    if (bootstrap.length === 0) {
        emptyState.classList.remove("hidden");
    }
    bootstrap.forEach((signal) => addSignal(signal));

    // refresh "time ago" labels periodically without re-fetching anything
    setInterval(() => {
        document.querySelectorAll("[data-timestamp]").forEach((el) => {
            el.textContent = `Received ${timeAgo(el.dataset.timestamp)}`;
        });
    }, 5000);

    // ---- WebSocket with backoff reconnection ----
    let retryDelay = 1000;
    const MAX_RETRY_DELAY = 15000;

    function connect() {
        setStatus("connecting");
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/signals/`);

        ws.onopen = () => {
            setStatus("connected");
            retryDelay = 1000;
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "signal.created" && data.signal) {
                addSignal(data.signal, { animate: true, prepend: true });
                updateCount(1);
            }
        };

        ws.onclose = () => {
            setStatus("disconnected");
            setTimeout(connect, retryDelay);
            retryDelay = Math.min(retryDelay * 2, MAX_RETRY_DELAY);
        };

        ws.onerror = () => ws.close();
    }

    connect();
})();
