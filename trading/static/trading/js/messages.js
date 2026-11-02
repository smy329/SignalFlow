/* Messages page: show times in the browser's timezone and, on the first page, add new messages
   as they arrive and refresh the status of ones still being processed (every 3 seconds). */
(function () {
  const tbody = document.getElementById("message-rows");
  const pager = document.getElementById("pager");
  const onFirstPage = document.getElementById("live-config").dataset.page === "1";

  function formatTimes(root) {
    root.querySelectorAll("time[data-iso]").forEach(function (t) {
      t.textContent = new Date(t.dataset.iso).toLocaleString(undefined, {
        month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    });
  }

  /* Make our rows match the freshly fetched ones: keep rows that are unchanged (so selected text
     survives), update changed ones, insert new ones in order and drop the ones that fell off the page. */
  function syncRows(freshBody) {
    const fresh = Array.from(freshBody.querySelectorAll("tr[data-id]"));
    if (!fresh.length) {
      if (!tbody.querySelector("tr[data-empty]")) tbody.innerHTML = freshBody.innerHTML;
      return;
    }
    tbody.querySelectorAll("tr[data-empty]").forEach((row) => row.remove());

    let cursor = tbody.firstElementChild;
    fresh.forEach(function (row) {
      if (cursor && cursor.dataset.id === row.dataset.id) {
        if (cursor.innerHTML !== row.innerHTML) {
          cursor.innerHTML = row.innerHTML;
          formatTimes(cursor);
        }
        cursor = cursor.nextElementSibling;
      } else {
        const node = document.importNode(row, true);
        node.classList.add("row-new");
        tbody.insertBefore(node, cursor);
        formatTimes(node);
      }
    });
    while (cursor) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
  }

  formatTimes(document);

  window.LiveUI.every(3000, async function () {
    const doc = await window.LiveUI.fetchDoc(window.location.href);
    window.LiveUI.syncLive(doc);  // the "N received" counter
    const freshPager = doc.getElementById("pager");
    if (freshPager && pager.innerHTML !== freshPager.innerHTML) pager.innerHTML = freshPager.innerHTML;
    if (onFirstPage) syncRows(doc.getElementById("message-rows"));
  });
})();
