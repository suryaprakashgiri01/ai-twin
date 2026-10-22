// Ingest: a typed note, files, web pages, GitHub repositories or the LinkedIn export. Each
// submitted source is polled until it is done, quarantined or failed.

import { api } from "../api.js";
import { badge, clear, errorLine, h, select } from "../dom.js";
import { CATEGORIES, TIERS, field, head, list, subtabs, values } from "./common.js";

const FINAL = new Set(["done", "quarantined", "failed"]);
let tracker;

export function mount(root) {
  tracker = h("div", { class: "card", hidden: true });
  root.append(head("Ingest", "Add knowledge. Everything starts private (or the tier you pick) and waits in Review."));
  subtabs(root, { Note: note, Files: files, URLs: urls, GitHub: github, LinkedIn: linkedin });
  root.append(tracker);
}

function common(withTitle = false) {
  return [
    withTitle ? field("Title", h("input", { name: "title", required: true, maxLength: 200 })) : null,
    h("div", { class: "row" },
      field("Category", select(CATEGORIES, "work-note", { name: "category" })),
      field("Tier", select([["", "auto (private)"], ...TIERS.map((t) => [t, t])], "", { name: "tier" })),
      field("Occurred", h("input", { type: "date", name: "occurredAt" })),
      field("Tags", h("input", { name: "tags", placeholder: "comma, separated" }))),
    field("Summary", h("input", { name: "summary", maxLength: 500 }), "optional"),
  ];
}

function submitForm(controls, send) {
  const slot = h("div");
  const button = h("button", { class: "btn btn-primary", type: "submit", text: "Ingest" });
  const form = h("form", { class: "card form", on: { submit: async (e) => {
    e.preventDefault();
    button.disabled = true;
    const r = await send(values(form), form);
    button.disabled = false;
    if (!r.ok) { clear(slot, errorLine(r)); return; }
    clear(slot);
    const sources = r.data.sources || [r.data];
    track(sources);
  } } }, controls, h("div", { class: "row" }, button), slot);
  return form;
}

function meta(v) {
  return { category: v.category, tier: v.tier, occurredAt: v.occurredAt, summary: v.summary, tags: list(v.tags) };
}

function note() {
  return submitForm([...common(true), field("Text", h("textarea", { name: "body", rows: 8, required: true }))],
    (v) => api.post("/admin/v1/ingest/text", { ...meta(v), title: v.title, body: v.body }));
}

function files() {
  return submitForm([field("Files", h("input", { type: "file", name: "files", multiple: true, required: true })), ...common()],
    (v) => {
      const form = new FormData();
      for (const file of v.files) form.append("files", file);
      form.append("category", v.category);
      if (v.tier) form.append("tier", v.tier);
      if (v.occurredAt) form.append("occurredAt", v.occurredAt);
      if (v.summary) form.append("summary", v.summary);
      if (v.tags) form.append("tags", v.tags);
      return api.upload("/admin/v1/ingest/files", form);
    });
}

function urls() {
  return submitForm([field("URLs", h("textarea", { name: "urls", rows: 3, required: true, placeholder: "one per line" })), ...common(),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "refresh" }), h("span", { text: "Fetch again even if unchanged" }))],
  (v) => api.post("/admin/v1/ingest/urls", { ...meta(v), urls: v.urls.split(/\s+/).filter(Boolean), refresh: v.refresh }));
}

function github() {
  return submitForm([field("Repositories", h("input", { name: "repos", required: true, placeholder: "owner/name, owner/other" }), "only allowlisted repositories are synced"),
    field("Tags", h("input", { name: "tags" })),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "refresh" }), h("span", { text: "Sync again even if unchanged" }))],
  (v) => api.post("/admin/v1/ingest/github", { repos: list(v.repos), tags: list(v.tags), refresh: v.refresh }));
}

function linkedin() {
  return submitForm([field("LinkedIn export (.zip)", h("input", { type: "file", name: "file", accept: ".zip", required: true }))],
    (v) => {
      const form = new FormData();
      form.append("file", v.file[0]);
      return api.upload("/admin/v1/ingest/linkedin-export", form);
    });
}

function track(sources) {
  tracker.hidden = false;
  clear(tracker, h("div", { class: "card-title" }, h("strong", { text: "Submitted" })));
  for (const s of sources) {
    const line = h("div", { class: "source-line" });
    tracker.append(line);
    poll(s.sourceId, line, s);
  }
}

async function poll(id, line, first) {
  let status = first;
  for (let n = 0; n < 90; n += 1) {
    paint(line, id, status);
    if (FINAL.has(status.status)) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const r = await api.get(`/admin/v1/sources/${id}`);
    if (!r.ok) { clear(line, errorLine(r)); return; }
    status = r.data;
  }
}

function paint(line, id, s) {
  const tone = s.status === "done" ? "ok" : s.status === "failed" ? "err" : s.status === "quarantined" ? "warn" : "info";
  clear(line, badge(s.status, tone), " ", h("span", { class: "mono", text: s.filename || s.uri || id }),
    s.duplicate ? badge("duplicate", "") : null,
    s.errorCode ? h("span", { class: "muted small", text: ` ${s.errorCode}: ${s.errorMessage || ""}` }) : null,
    s.documentIds?.length ? h("span", { class: "muted small", text: ` → ${s.documentIds.length} document(s), see Review` }) : null);
}
