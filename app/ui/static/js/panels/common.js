// Shared pieces for the API tabs: a paged, searchable, filterable list and small form helpers.

import { api } from "../api.js";
import { clear, empty, errorLine, h, select, table } from "../dom.js";

/**
 * A list page over one of the API's paged routes.
 * listView({ path, columns, filters: { field: [values] }, sort, onRow, pageSize, emptyText })
 * Returns { el, reload }.
 */
export function listView({ path, columns, filters = {}, sort, onRow, pageSize = 20, emptyText = "Nothing here yet." }) {
  const state = { page: 1, q: "", filter: {} };
  const body = h("div", { class: "list-body" });
  const pager = h("div", { class: "row pager" });
  const search = h("input", { type: "search", placeholder: "Search", on: { change: (e) => { state.q = e.target.value.trim(); state.page = 1; reload(); } } });
  const pickers = Object.entries(filters).map(([field, values]) =>
    h("label", { class: "inline" }, h("span", { text: field }),
      select([["", "any"], ...values.map((v) => [v, v])], "", { on: { change: (e) => { state.filter[field] = e.target.value; state.page = 1; reload(); } } })));
  const el = h("div", { class: "card" },
    h("div", { class: "row" }, search, pickers, h("span", { class: "spacer" }),
      h("button", { class: "btn btn-sm", type: "button", text: "Refresh", on: { click: () => reload() } })),
    body, pager);

  async function reload() {
    const filter = Object.entries(state.filter).filter(([, v]) => v).map(([k, v]) => `${k}:${v}`).join(",");
    const query = { page: state.page, pageSize, q: state.q, filter, ...(sort ? { sortColumn: sort[0], sortOrder: sort[1] } : {}) };
    const r = await api.get(path, query);
    if (!r.ok) { clear(body, errorLine(r)); clear(pager); return r; }
    const page = r.data;
    const items = page.listOfItems || [];
    clear(body, items.length ? table(items, columns, onRow) : empty(emptyText));
    clear(pager,
      h("span", { class: "muted small", text: `${page.total} total · page ${page.page} of ${Math.max(page.totalPages, 1)}` }),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-sm", type: "button", text: "‹ Prev", disabled: page.page <= 1, on: { click: () => { state.page -= 1; reload(); } } }),
      h("button", { class: "btn btn-sm", type: "button", text: "Next ›", disabled: page.page >= page.totalPages, on: { click: () => { state.page += 1; reload(); } } }));
    return r;
  }
  return { el, reload };
}

/** A labelled field. `input` is any control. */
export function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("span", { class: "muted small", text: hint }) : null);
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
