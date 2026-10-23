// Tiny DOM helpers. Text always goes through textContent: no HTML strings, no XSS.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "on") for (const [ev, fn] of Object.entries(value)) el.addEventListener(ev, fn);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function badge(text, tone = "") {
  return h("span", { class: `badge ${tone ? `b-${tone}` : ""}`, text });
}

export function json(value) {
  return h("pre", { class: "json", text: JSON.stringify(value, null, 2) });
}

export function kv(pairs) {
  const dl = h("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v === undefined) continue;
    dl.append(h("dt", { text: k }), h("dd", {}, v instanceof Node ? v : String(v ?? "—")));
  }
  return dl;
}

export function empty(message, ctaLabel, onClick) {
  return h("div", { class: "empty" },
    h("div", { text: message }),
    ctaLabel ? h("button", { class: "btn btn-primary cta", type: "button", text: ctaLabel, on: { click: onClick } }) : null);
}

export async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try { return await fn(); } finally { button.disabled = false; button.textContent = label; }
}

let toastTimer;
export function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
}

export function time(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** A select whose options are [value, label] pairs (or plain values). */
export function select(options, value, attrs = {}) {
  const el = h("select", attrs);
  for (const opt of options) {
    const [v, label] = Array.isArray(opt) ? opt : [opt, opt];
    el.append(h("option", { value: v, text: label, selected: v === value }));
  }
  return el;
}

/** A small error line from an API result. */
export function errorLine(r) {
  const code = r.error?.code || (r.status === 0 ? "NETWORK_ERROR" : `HTTP ${r.status}`);
  return h("div", { class: "error-line", text: `${code}: ${r.message || "Request failed"}` });
}

/** Build a table from rows and [header, cell(row), sortKey?] columns. `opts.sort` is
 * [column, order] and `opts.onSort(key)` makes headers with a sort key clickable;
 * `opts.selected(row)` marks the selected row. */
export function table(rows, columns, onRow, opts = {}) {
  const [sortKey, sortOrder] = opts.sort || [];
  const head = h("tr", {}, columns.map(([title, , key]) => {
    if (!key || !opts.onSort) return h("th", { text: title });
    const active = key === sortKey;
    return h("th", { class: `sortable ${active ? "sorted" : ""}`, "aria-sort": active ? (sortOrder === "asc" ? "ascending" : "descending") : "none" },
      h("button", { class: "th-sort", type: "button", on: { click: () => opts.onSort(key) } },
        title, " ", sortIcon(active ? sortOrder : null)));
  }));
  const body = h("tbody", {}, rows.map((row) => {
    const tr = h("tr", { class: onRow ? "clickable" : "" },
      columns.map(([, cell]) => {
        const value = cell(row);
        return h("td", {}, value instanceof Node ? value : String(value ?? "—"));
      }));
    if (opts.selected?.(row)) tr.classList.add("selected");
    if (onRow) tr.addEventListener("click", () => {
      for (const other of tr.parentNode.children) other.classList.remove("selected");
      tr.classList.add("selected");
      onRow(row);
    });
    return tr;
  }));
  return h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, head), body));
}

function sortIcon(order) {
  return h("span", { class: `sort-mark ${order ? "on" : ""}`, text: order === "asc" ? "▲" : order === "desc" ? "▼" : "↕" });
}

/** "reviewStatus" → "Review status", "maxTier" → "Max tier". */
export function humanize(key) {
  const words = String(key).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A button that is an icon (with a tooltip and aria-label), optionally with text. It pops when
 * clicked; `spin` keeps the icon turning while the returned promise of `onClick` runs. */
export function iconButton(iconEl, label, onClick, { text, cls = "", spin = false, type = "button" } = {}) {
  const button = h("button", { class: `btn icon-button ${cls}`.trim(), type, title: label, "aria-label": label }, iconEl, text ? h("span", { text }) : null);
  if (onClick) {
    button.addEventListener("click", async (event) => {
      button.classList.remove("pop");
      void button.offsetWidth; // restart the animation
      button.classList.add("pop");
      if (!spin) { onClick(event); return; }
      button.classList.add("spinning");
      button.disabled = true;
      try { await onClick(event); } finally { button.classList.remove("spinning"); button.disabled = false; }
    });
  }
  return button;
}
