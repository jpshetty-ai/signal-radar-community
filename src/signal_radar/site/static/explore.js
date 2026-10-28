// Explore: radial neighbourhood graph from static JSON (data/entities.json, data/entity/<slug>.json).
// Single-file builds embed that data as window.SR_DATA and address entities as #explore:<slug>.
(() => {
  const DATA = window.SR_DATA || null;
  const PREFIX = DATA ? "explore:" : "";
  const slugFromHash = () => {
    const h = location.hash.slice(1);
    return PREFIX ? (h.startsWith(PREFIX) ? h.slice(PREFIX.length) : "") : h;
  };
  const goTo = slug => { location.hash = PREFIX + slug; };
  const NS = "http://www.w3.org/2000/svg";
  const W = 720, H = 560, CX = W / 2, CY = H / 2, R1 = 165, R2 = 250;
  const svg = document.getElementById("graph");
  const list = document.getElementById("ent-list");
  const search = document.getElementById("ent-search");
  const typesBox = document.getElementById("ent-types");
  const panel = document.getElementById("ent-panel");
  const SRC = { tldr_ai: "TLDR AI", tldr_tech: "TLDR Tech", tldr_founders: "TLDR Founders", alphasignal: "AlphaSignal", bayarealetters: "Bay Area Letters", aiact_newsletter: "EU AI Act Newsletter", sifted: "Sifted", techeu: "Tech.eu", ec_digital: "EC Digital Strategy" };
  let index = [], activeType = null, current = null;

  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const h = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };

  function renderList() {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    index.filter(e => (!activeType || e.type === activeType) && (!q || e.name.toLowerCase().includes(q)))
      .slice(0, 120).forEach(e => {
        const b = h("button");
        b.type = "button";
        b.append(h("b", e.name), h("span", `${e.mentions}`));
        b.firstChild.style.fontWeight = "500";
        if (current && e.slug === current) b.setAttribute("aria-current", "true");
        b.addEventListener("click", () => goTo(e.slug));
        list.appendChild(b);
      });
  }

  function renderTypes() {
    const types = [...new Set(index.map(e => e.type))].sort();
    typesBox.replaceChildren();
    for (const t of [null, ...types]) {
      const b = h("button", t || "All");
      b.type = "button";
      b.setAttribute("aria-pressed", String(activeType === t));
      b.addEventListener("click", () => { activeType = t; renderTypes(); renderList(); });
      typesBox.appendChild(b);
    }
  }

  function polar(r, i, n, offset = 0) {
    const a = -Math.PI / 2 + offset + (2 * Math.PI * i) / Math.max(n, 1);
    return [CX + r * Math.cos(a), CY + r * Math.sin(a), a];
  }

  function sparkline(series, w = 300, h = 46) {
    const s = el("svg", { width: "100%", viewBox: `0 0 ${w} ${h}`, class: "spark", role: "img",
      "aria-label": `weekly mentions, 12 weeks: ${series.join(", ")}` });
    const mx = Math.max(1, ...series), step = (w - 8) / (series.length - 1);
    const pts = series.map((v, i) => `${4 + i * step},${h - 6 - (v / mx) * (h - 14)}`).join(" ");
    el("polyline", { points: pts, fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linejoin": "round" }, s);
    series.forEach((v, i) => el("circle", { cx: 4 + i * step, cy: h - 6 - (v / mx) * (h - 14), r: i === series.length - 1 ? 2.6 : 1.2, fill: "currentColor" }, s));
    return s;
  }

  function renderGraph(d) {
    svg.replaceChildren();
    el("circle", { cx: CX, cy: CY, r: R1, class: "ring" }, svg);
    el("circle", { cx: CX, cy: CY, r: R2, class: "ring" }, svg);
    const nb = d.neighbours, maxW = Math.max(1, ...nb.map(n => n.weight));
    const pos = {};
    nb.forEach((n, i) => { pos[n.slug] = polar(R1, i, nb.length); });
    const items = d.items, ipos = items.map((_, i) => polar(R2, i, items.length, Math.PI / Math.max(items.length, 1)));

    const edges = el("g", {}, svg);
    items.forEach((it, i) => {
      el("line", { x1: CX, y1: CY, x2: ipos[i][0], y2: ipos[i][1], class: "edge item" }, edges);
      it.links.forEach(s => pos[s] && el("line", { x1: pos[s][0], y1: pos[s][1], x2: ipos[i][0], y2: ipos[i][1], class: "edge item" }, edges));
    });
    nb.forEach(n => el("line", { x1: CX, y1: CY, x2: pos[n.slug][0], y2: pos[n.slug][1], class: "edge",
      "stroke-width": (1 + 3 * n.weight / maxW).toFixed(2) }, edges));

    items.forEach((it, i) => {
      const g = el("g", { class: "node-item" + ((it.relevance || 0) >= 2 ? " relevant" : ""), tabindex: 0 }, svg);
      el("rect", { x: ipos[i][0] - 5, y: ipos[i][1] - 5, width: 10, height: 10, rx: 1 }, g);
      el("title", {}, g).textContent = `${it.title} (${it.date}, ${SRC[it.source] || it.source})`;
      g.addEventListener("click", () => window.open(it.url, "_blank", "noopener"));
    });

    nb.forEach(n => {
      const [x, y, a] = pos[n.slug];
      const r = 5 + 9 * Math.sqrt(n.weight / maxW);
      const g = el("g", { class: "node-entity", tabindex: 0 }, svg);
      el("circle", { cx: x, cy: y, r }, g);
      const right = Math.cos(a) >= 0;
      const t = el("text", { x: x + (right ? r + 6 : -r - 6), y: y + 4, "text-anchor": right ? "start" : "end" }, g);
      t.textContent = n.name;
      el("title", {}, g).textContent = `${n.name} (${n.type}), ${n.weight} shared items`;
      const go = () => goTo(n.slug);
      g.addEventListener("click", go);
      g.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
    });

    const c = el("g", { class: "node-entity center" }, svg);
    el("circle", { cx: CX, cy: CY, r: 20 }, c);
    el("text", { x: CX, y: CY + 40, "text-anchor": "middle" }, c).textContent = d.name;
  }

  function renderPanel(d) {
    panel.replaceChildren();
    panel.append(h("span", d.type, "eyebrow"), h("h2", d.name));
    const facts = h("div", null, "facts");
    facts.append(h("span", `${d.mentions} mentions`), h("span", `first seen ${d.first_seen}`));
    panel.append(facts, sparkline(d.series), h("p", "Weekly mentions, last 12 weeks", "hint"));
    if (d.relations.length) {
      const rel = h("div", null, "rels");
      d.relations.forEach(r => rel.append(h("span", `${r.source} ${r.relation} ${r.target}${r.count > 1 ? ` ×${r.count}` : ""}`)));
      panel.append(h("h3", "Stated relations"), rel);
    }
    panel.append(h("h3", `News · ${d.window_days} days`));
    const ol = document.createElement("ol");
    d.items.forEach(it => {
      const li = document.createElement("li");
      const a = h("a", it.title); a.href = it.url; a.target = "_blank"; a.rel = "noopener";
      li.append(a, h("span", ` ${it.date} · ${SRC[it.source] || it.source}${(it.relevance || 0) >= 2 ? " · relevant" : ""}`, "hint"));
      ol.appendChild(li);
    });
    if (!d.items.length) ol.append(h("li", "No items in this window."));
    panel.appendChild(ol);
    panel.querySelectorAll("h3").forEach(x => { x.style.marginTop = "16px"; });
  }

  async function show(slug) {
    try {
      let d;
      if (DATA) {
        d = DATA.entity[slug];
        if (!d) throw new Error("missing");
      } else {
        const r = await fetch(`data/entity/${slug}.json`);
        if (!r.ok) throw new Error(r.status);
        d = await r.json();
      }
      current = slug;
      renderGraph(d); renderPanel(d); renderList();
    } catch (e) {
      panel.replaceChildren(h("p", `No neighbourhood for “${slug}” (entities need at least 3 mentions).`, "empty"));
    }
  }

  async function init() {
    index = DATA ? DATA.entities : await (await fetch("data/entities.json")).json();
    renderTypes(); renderList();
    search.addEventListener("input", renderList);
    window.addEventListener("hashchange", () => { const s = slugFromHash(); if (s) show(s); });
    show(slugFromHash() || (index[0] && index[0].slug));
  }
  init();
})();
