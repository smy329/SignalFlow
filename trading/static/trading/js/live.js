/* Live updates without page refresh.
   The server keeps rendering the pages (so formatting lives in one place); the browser re-fetches the
   page in the background and copies over only what changed. Polling pauses while the tab is hidden. */
window.LiveUI = (function () {
  /* Run `task` every `ms`; never overlaps itself, skips hidden tabs, catches up when the tab returns. */
  function every(ms, task) {
    let busy = false;
    async function tick() {
      if (busy || document.hidden) return;
      busy = true;
      try { await task(); } catch (e) { /* network hiccup: try again next tick */ } finally { busy = false; }
    }
    setInterval(tick, ms);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
  }

  /* Fetch a page of this site and parse it. A redirect to the login page (expired session) throws. */
  async function fetchDoc(url) {
    const response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
    if (!response.ok || response.url.includes("/login/")) throw new Error("not available");
    return new DOMParser().parseFromString(await response.text(), "text/html");
  }

  /* For every [data-live=key] on this page, copy text and classes from the same key in `doc`. */
  function syncLive(doc) {
    document.querySelectorAll("[data-live]").forEach(function (el) {
      const fresh = doc.querySelector('[data-live="' + CSS.escape(el.dataset.live) + '"]');
      if (!fresh) return;
      if (el.textContent !== fresh.textContent) el.textContent = fresh.textContent;
      if (el.className !== fresh.className) el.className = fresh.className;
    });
  }

  return { every: every, fetchDoc: fetchDoc, syncLive: syncLive };
})();
