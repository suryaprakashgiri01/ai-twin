// Ingest: a typed note, files, web pages, GitHub repositories or the LinkedIn export. Each
// submitted source is polled until it is done, quarantined or failed.

import { api } from "../api.js";
import { badge, clear, errorLine, h, iconButton, select, toast } from "../dom.js";
import { icon } from "../icons.js";
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
  const tier = select([["", "Auto (private)"], ...TIERS.map((t) => [t, t.charAt(0).toUpperCase() + t.slice(1)])], "", { name: "tier" });
  const warn = h("div", { class: "warn-line", hidden: true }, icon("alert", { size: 14 }),
    h("span", { text: "Public: once you approve and publish it, anyone on the portfolio can read it. Never mark employer or private data public." }));
  tier.addEventListener("change", () => { warn.hidden = tier.value !== "public"; });
  return [
    withTitle ? field("Title", h("input", { name: "title", required: true, maxLength: 200, placeholder: "e.g. System design interview prep" })) : null,
    h("div", { class: "row" },
      field("Category", select(CATEGORIES, "work-note", { name: "category" })),
      field("Tier", tier),
      field("Occurred", h("input", { type: "date", name: "occurredAt" })),
      field("Tags", h("input", { name: "tags", placeholder: "comma, separated" }))),
    warn,
    field("Summary", h("input", { name: "summary", maxLength: 500, placeholder: "One line on what this is (helps search and review)" })),
  ];
}

function submitForm(controls, send) {
  const slot = h("div");
  const button = iconButton(icon("upload"), "Ingest", null, { cls: "btn-primary", type: "submit", text: "Ingest" });
  const form = h("form", { class: "card form", on: { submit: async (e) => {
    e.preventDefault();
    button.disabled = true;
    const r = await send(values(form), form);
    button.disabled = false;
    if (!r) return; // the form reported its own problem
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
  return submitForm([...common(true), field("Text", h("textarea", { name: "body", rows: 8, required: true, placeholder: "Markdown is fine" }))],
    (v) => api.post("/admin/v1/ingest/text", { ...meta(v), title: v.title, body: v.body }));
}

// A drop zone over a real file input: drag files in, or pick them; the list shows what is chosen.
function dropZone(name, { multiple = true, accept } = {}) {
  const input = h("input", { type: "file", name, multiple, accept, class: "file-input", tabIndex: -1 });
  const list = h("ul", { class: "file-list" });
  const clearBtn = iconButton(icon("x", { size: 14 }), "Clear the selection", () => { input.value = ""; paint(); }, { cls: "btn-sm", text: "Clear" });
  const pick = iconButton(icon("upload", { size: 16 }), "Choose files", () => input.click(), { cls: "btn-pick", text: multiple ? "Choose files" : "Choose file" });
  const zone = h("div", { class: "drop-zone", tabIndex: 0 },
    icon("upload", { size: 28, cls: "drop-icon" }),
    h("div", { text: multiple ? "Drag files here" : "Drag the file here" }), h("div", { class: "muted small", text: "or" }),
    h("div", { class: "row" }, pick, clearBtn), list, input);
  function paint() {
    const files = [...input.files];
    clearBtn.hidden = !files.length;
    clear(list, files.map((f) => h("li", {}, icon("file", { size: 13 }), h("span", { text: f.name }), h("span", { class: "muted small", text: ` ${Math.ceil(f.size / 1024)} KB` }))));
  }
  input.addEventListener("change", paint);
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    const dt = new DataTransfer();
    for (const f of [...(multiple ? input.files : []), ...e.dataTransfer.files].slice(0, multiple ? 50 : 1)) dt.items.add(f);
    input.files = dt.files;
    paint();
  });
  paint();
  return zone;
}

function files() {
  return submitForm([field("Files", dropZone("files"), null, { required: true }), ...common()],
    (v) => {
      if (!v.files?.length) { toast("Choose at least one file"); return null; }
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

// URLs as chips: type or paste, Enter adds; each chip opens in a new tab or is removed.
function urlChips(name) {
  const hidden = h("input", { type: "hidden", name });
  const chips = h("div", { class: "url-chips" });
  const input = h("input", { type: "url", placeholder: "https://… then Enter (paste several at once)", "aria-label": "Add a URL" });
  let urls = [];
  const sync = () => {
    hidden.value = urls.join("\n");
    clear(chips, urls.map((u) => h("span", { class: "chip url-chip" },
      h("a", { href: u, target: "_blank", rel: "noopener noreferrer", title: "Open in a new tab" }, icon("external", { size: 12 })),
      h("span", { class: "url-text", text: u }),
      h("button", { class: "chip-x", type: "button", "aria-label": `Remove ${u}`, on: { click: () => { urls = urls.filter((x) => x !== u); sync(); } } }, icon("x", { size: 12 })))));
  };
  const add = (text) => {
    for (const raw of text.split(/[\s,]+/)) {
      const u = raw.trim();
      if (!u) continue;
      if (!/^https?:\/\/\S+\.\S+/.test(u)) { toast(`Not a web address: ${u}`); continue; }
      if (!urls.includes(u)) urls.push(u);
    }
    input.value = "";
    sync();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(input.value); } });
  input.addEventListener("paste", (e) => { e.preventDefault(); add(e.clipboardData.getData("text")); });
  input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });
  return h("div", { class: "url-box" }, chips, h("div", { class: "search-box" }, icon("link", { size: 14 }), input), hidden);
}

function refreshBox(label, warning) {
  const box = h("input", { type: "checkbox", name: "refresh" });
  const warn = h("div", { class: "warn-line", hidden: true }, icon("alert", { size: 14 }), h("span", { text: warning }));
  box.addEventListener("change", () => { warn.hidden = !box.checked; });
  return [h("label", { class: "check" }, box, h("span", { text: label })), warn];
}

function urls() {
  return submitForm([field("URLs", urlChips("urls"), null, { required: true }), ...common(),
    ...refreshBox("Fetch again even if unchanged", "Fetches every page again and replaces its documents: their labels, approval and publishing are dropped, and the new documents wait in Review.")],
  (v) => {
    const list = (v.urls || "").split("\n").filter(Boolean);
    if (!list.length) { toast("Add at least one URL"); return null; }
    return api.post("/admin/v1/ingest/urls", { ...meta(v), urls: list, refresh: v.refresh });
  });
}

function github() {
  return submitForm([field("Repositories", h("input", { name: "repos", required: true, placeholder: "owner/name, owner/other" }), "Only repositories in GITHUB_SYNC_REPOS are synced"),
    field("Tags", h("input", { name: "tags", placeholder: "comma, separated" })),
    ...refreshBox("Sync again even if unchanged", "Re-syncs every listed repository and replaces its documents: their labels, approval and publishing are dropped, and the new documents wait in Review.")],
  (v) => api.post("/admin/v1/ingest/github", { repos: list(v.repos), tags: list(v.tags), refresh: v.refresh }));
}

function linkedin() {
  return submitForm([field("LinkedIn export (.zip)", dropZone("file", { multiple: false, accept: ".zip" }), "LinkedIn → Settings → Data privacy → Get a copy of your data", { required: true })],
    (v) => {
      if (!v.file?.length) { toast("Choose the export ZIP"); return null; }
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
