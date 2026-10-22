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

/** Build a table from rows and [header, cell(row)] columns. */
export function table(rows, columns, onRow) {
  const head = h("tr", {}, columns.map(([title]) => h("th", { text: title })));
  const body = h("tbody", {}, rows.map((row) => h("tr",
    { class: onRow ? "clickable" : "", on: onRow ? { click: () => onRow(row) } : undefined },
    columns.map(([, cell]) => {
      const value = cell(row);
      return h("td", {}, value instanceof Node ? value : String(value ?? "—"));
    }))));
  return h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, head), body));
}
