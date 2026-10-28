// Console-only behaviour: feedback votes and the Ask drawer. Talks to the local server (signal-radar web).
(() => {
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  };

  // ---- votes: + marks useful, − hides the item from the reading list on the next build
  async function syncVotes() {
    try {
      const votes = await (await fetch("api/votes")).json();
      document.querySelectorAll(".read").forEach(li => {
        const v = votes[li.dataset.id] || 0;
        li.querySelectorAll(".vote button").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.vote) === v)));
        li.classList.toggle("hidden-vote", v === -1);
      });
    } catch (e) { /* static copy without a server: buttons keep their built state */ }
  }
  document.querySelectorAll(".read .vote button").forEach(b => b.addEventListener("click", async () => {
    const li = b.closest(".read");
    const vote = b.getAttribute("aria-pressed") === "true" ? 0 : Number(b.dataset.vote);
    try {
      await post("api/feedback", { item_id: li.dataset.id, vote });
      li.querySelectorAll(".vote button").forEach(x => x.setAttribute("aria-pressed", String(Number(x.dataset.vote) === vote)));
      li.classList.toggle("hidden-vote", vote === -1);
    } catch (e) { b.title = "Could not save: is `signal-radar web` running?"; }
  }));
  syncVotes();

  // ---- Ask drawer
  const drawer = document.getElementById("ask"), log = document.getElementById("ask-log");
  const form = document.getElementById("ask-form"), q = document.getElementById("ask-q");
  const open = () => { drawer.hidden = false; q.focus(); };
  document.getElementById("ask-open")?.addEventListener("click", open);
  document.getElementById("ask-close")?.addEventListener("click", () => { drawer.hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") drawer.hidden = true; });

  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };

  function renderAnswer(box, a) {
    box.replaceChildren();
    const ids = Object.fromEntries((a.cited || []).map((c, i) => [c.id, i + 1]));
    const p = el("p");
    // Turn [item-id] citations into numbered links to the source list below.
    (a.answer || "").split(/(\[[0-9a-f]{16}\])/).forEach(part => {
      const m = part.match(/^\[([0-9a-f]{16})\]$/);
      if (m && ids[m[1]]) { const s = el("a", `[${ids[m[1]]}]`, "cite"); s.href = `#src-${m[1]}`; p.append(s); }
      else if (!m) p.append(part);
    });
    box.append(p);
    if (a.evidence_is_thin) box.append(el("div", "Evidence is thin for this question.", "thin"));
    if (a.cited && a.cited.length) {
      const ol = el("ol", null, "sources");
      a.cited.forEach(c => {
        const li = el("li"); li.id = `src-${c.id}`;
        const link = el("a", c.title); link.href = c.url; link.target = "_blank"; link.rel = "noopener";
        li.append(link, el("span", ` ${c.date}`, "hint"));
        ol.append(li);
      });
      box.append(ol);
    }
    const tr = el("ul", null, "trace");
    (a.trace || []).forEach(t => tr.append(el("li", t)));
    box.append(tr);
  }

  form?.addEventListener("submit", async e => {
    e.preventDefault();
    const question = q.value.trim();
    if (!question) return;
    log.querySelector(".empty")?.remove();
    const turn = el("div"); const box = el("div", "Reading the corpus…", "a");
    turn.append(el("div", question, "q"), box); log.append(turn);
    q.value = ""; turn.scrollIntoView({ block: "end" });
    try { renderAnswer(box, await post("api/ask", { question })); }
    catch (err) { box.textContent = `No answer: ${err.message}. Is the local server running (signal-radar web)?`; }
    turn.scrollIntoView({ block: "end" });
  });
})();
