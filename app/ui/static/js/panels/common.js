// Shared pieces for the API tabs: a paged, searchable, filterable list and small form helpers.

import { api } from "../api.js";
import { clear, empty, errorLine, h, humanize, iconButton, select, table } from "../dom.js";
import { icon } from "../icons.js";

/**
 * A list page over one of the API's paged routes: search, filters (with defaults), sortable
 * headers, page size, first / previous / page / next / last, and a refresh icon that spins.
 * listView({ path, columns: [[title, cell, sortKey?]], filters: { field: [values] },
 *   defaults: { field: value }, sort: [column, order], onRow, autoSelect, pageSize, emptyText })
 * Returns { el, reload }.
 */
export function listView({ path, columns, filters = {}, defaults = {}, sort, onRow, autoSelect = false, pageSize = 10, emptyText = "Nothing here yet." }) {
  const state = { page: 1, size: pageSize, q: "", filter: { ...defaults }, sort: sort || null, selected: null };
  const body = h("div", { class: "list-body" });
  const pager = h("div", { class: "pager" });
  const search = h("input", { type: "search", placeholder: "Search…", "aria-label": "Search",
    on: { change: (e) => { state.q = e.target.value.trim(); state.page = 1; reload(); } } });
  const pickers = Object.entries(filters).map(([field, values]) =>
    h("label", { class: "inline" }, h("span", { text: humanize(field) }),
      select([["", "Any"], ...values.map((v) => [v, humanize(v)])], defaults[field] || "",
        { on: { change: (e) => { state.filter[field] = e.target.value; state.page = 1; reload(); } } })));
  const refresh = iconButton(icon("refresh"), "Refresh", () => reload(), { cls: "btn-sm", spin: true });
  const el = h("div", { class: "card" },
    h("div", { class: "toolbar" }, h("div", { class: "search-box" }, icon("search", { size: 14 }), search), pickers, h("span", { class: "spacer" }), refresh),
    body, pager);

  async function reload() {
    const filter = Object.entries(state.filter).filter(([, v]) => v).map(([k, v]) => `${k}:${v}`).join(",");
    const query = { page: state.page, pageSize: state.size, q: state.q, filter, ...(state.sort ? { sortColumn: state.sort[0], sortOrder: state.sort[1] } : {}) };
    el.classList.add("loading");
    const r = await api.get(path, query);
    el.classList.remove("loading");
    if (!r.ok) { clear(body, errorLine(r)); clear(pager); return r; }
    const page = r.data;
    const items = page.listOfItems || [];
    const pick = (row) => { state.selected = row.id ?? null; onRow?.(row); };
    clear(body, items.length
      ? table(items, columns, onRow ? pick : null, {
        sort: state.sort,
        onSort: (key) => {
          const order = state.sort?.[0] === key && state.sort[1] === "desc" ? "asc" : "desc";
          state.sort = [key, order];
          reload();
        },
        selected: (row) => row.id !== undefined && row.id === state.selected,
      })
      : empty(emptyText));
    renderPager(page);
    if (autoSelect && onRow && items.length && !items.some((row) => row.id === state.selected)) {
      pick(items[0]);
      body.querySelector("tbody tr")?.classList.add("selected");
    }
    return r;
  }

  function renderPager(page) {
    const pages = Math.max(page.totalPages, 1);
    const go = (n) => { state.page = Math.min(Math.max(n, 1), pages); reload(); };
    const nav = (name, label, target, disabled) => {
      const button = iconButton(icon(name), label, () => go(target), { cls: "btn-sm" });
      button.disabled = disabled;
      return button;
    };
    const jump = select(Array.from({ length: pages }, (_, i) => [String(i + 1), `Page ${i + 1}`]), String(page.page),
      { "aria-label": "Page", on: { change: (e) => go(Number(e.target.value)) } });
    const size = select([10, 20, 50, 100].map((n) => [String(n), `${n} / page`]), String(state.size),
      { "aria-label": "Rows per page", on: { change: (e) => { state.size = Number(e.target.value); state.page = 1; reload(); } } });
    const from = page.total ? (page.page - 1) * page.pageSize + 1 : 0;
    const to = Math.min(page.page * page.pageSize, page.total);
    clear(pager,
      h("span", { class: "muted small", text: `${from}–${to} of ${page.total}` }),
      h("span", { class: "spacer" }),
      size,
      nav("first", "First page", 1, page.page <= 1),
      nav("prev", "Previous page", page.page - 1, page.page <= 1),
      jump,
      nav("next", "Next page", page.page + 1, page.page >= pages),
      nav("last", "Last page", pages, page.page >= pages));
  }
  return { el, reload };
}

/** A labelled field. `input` is any control. */
export function field(label, input, hint, { required: marked = false } = {}) {
  const required = marked || input?.required || input?.querySelector?.("[required]");
  return h("label", { class: "field" },
    h("span", {}, label, required ? h("span", { class: "req", "aria-hidden": "true", text: " *" }) : null),
    input, hint ? h("span", { class: "hint", text: hint }) : null);
}

/** Read named controls of a form into an object; empty strings become undefined. */
export function values(form) {
  const out = {};
  for (const el of form.querySelectorAll("[name]")) {
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "file") out[el.name] = el.files;
    else if (el.value.trim() !== "") out[el.name] = el.value.trim();
  }
  return out;
}

export const list = (text) => (text ? text.split(",").map((s) => s.trim()).filter(Boolean) : []);

/** The result line under a form: a success message or the API error. */
export function outcome(slot, r, success) {
  clear(slot, r.ok ? h("div", { class: "ok-line", text: success || r.message || "Done" }) : errorLine(r));
}

/** A panel heading with a short description. */
export function head(title, text, ...actions) {
  return h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: title }), h("p", { text })), h("span", { class: "spacer" }), actions);
}

/** Small segmented sub-tabs; `views` maps a name to a render function. */
export function subtabs(root, views, first) {
  const bar = h("div", { class: "subtabs" });
  const slot = h("div", { class: "subtab-body" });
  const showView = (name) => {
    for (const b of bar.children) b.setAttribute("aria-pressed", String(b.dataset.view === name));
    clear(slot, views[name]());
  };
  for (const name of Object.keys(views)) {
    bar.append(h("button", { class: "btn btn-sm", type: "button", dataset: { view: name }, text: name, on: { click: () => showView(name) } }));
  }
  root.append(bar, slot);
  showView(first || Object.keys(views)[0]);
}

export const CATEGORIES = ["resume", "project", "experience", "education", "certification", "skill", "interview", "application",
  "work-note", "compensation", "marksheet", "id-document", "profile-link", "blog", "persona", "assessment", "offer", "achievement",
  "testimonial", "job-description", "learning", "hr-call", "invite", "interview-questions", "common-questions", "question-bank",
  "company-details", "company-projects", "mixed", "other"];
export const TIERS = ["public", "private", "confidential", "restricted"];
