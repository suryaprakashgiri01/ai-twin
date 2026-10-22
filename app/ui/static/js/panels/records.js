// Records: the job-search log (applications, calls, interviews, offers). List, add, add a
// batch as JSON, edit and delete.

import { api } from "../api.js";
import { clear, h, select, toast } from "../dom.js";
import { field, head, listView, outcome, values } from "./common.js";

const KINDS = ["invite", "application", "hr_call", "assessment", "interview", "offer", "rejection", "withdrawal"];
const WORK = ["", "onsite", "remote", "hybrid"];
const COMPANY = ["", "product", "service", "research", "startup", "other"];
let view;
let editor;

export function mount(root) {
  editor = h("div", { class: "card" });
  view = listView({
    path: "/admin/v1/records",
    sort: ["date", "desc"],
    filters: { kind: KINDS, workMode: WORK.slice(1), companyType: COMPANY.slice(1) },
    columns: [["Date", (r) => (r.date || "").slice(0, 10), "date"], ["Kind", (r) => r.kind, "kind"], ["Company", (r) => r.company, "company"],
      ["Role", (r) => r.role, "role"], ["Stage", (r) => r.stage, "stage"], ["Platform", (r) => r.platform]],
    onRow: (r) => edit(r),
  });
  root.append(head("Records", "Every application, call, interview and offer. Ask questions about them in Chat."),
    h("div", { class: "split" }, view.el, editor));
  edit(null);
  view.reload();
}

export function shown() { view?.reload(); }

function edit(record) {
  const v = record || {};
  const slot = h("div");
  const form = h("form", { class: "form", on: { submit: (e) => { e.preventDefault(); save(record, form, slot); } } },
    h("div", { class: "card-title" }, h("strong", { text: record ? "Edit record" : "Add a record" }),
      record ? h("button", { class: "btn btn-sm", type: "button", text: "New instead", on: { click: () => edit(null) } }) : null),
    h("div", { class: "row" },
      record ? null : field("Kind", select(KINDS, "application", { name: "kind" })),
      field("Date", h("input", { type: "date", name: "date", value: v.date || new Date().toISOString().slice(0, 10), required: true })),
      field("Stage", h("input", { name: "stage", value: v.stage || "", required: !record, placeholder: "applied, screen, round 2…" }))),
    h("div", { class: "row" },
      field("Company", h("input", { name: "company", value: v.company || "", required: !record })),
      field("Role", h("input", { name: "role", value: v.role || "", required: !record }))),
    h("div", { class: "row" },
      field("Platform", h("input", { name: "platform", value: v.platform || "" })),
      field("Contact", h("input", { name: "contact", value: v.contact || "" })),
      field("Location", h("input", { name: "location", value: v.location || "" }))),
    h("div", { class: "row" },
      field("Work mode", select(WORK, v.workMode || "", { name: "workMode" })),
      field("Company type", select(COMPANY, v.companyType || "", { name: "companyType" }))),
    field("Notes", h("textarea", { name: "notes", rows: 3, text: v.notes || "" })),
    h("div", { class: "row" },
      h("button", { class: "btn btn-primary", type: "submit", text: record ? "Save" : "Add" }),
      record ? null : h("button", { class: "btn", type: "button", text: "Add to batch", title: "Append this record to the batch below, to add several at once", on: { click: () => toBatch(form) } }),
      record ? h("button", { class: "btn btn-danger", type: "button", text: "Delete", on: { click: () => remove(record) } }) : null),
    slot);
  clear(editor, form, record ? null : batch());
}

async function save(record, form, slot) {
  const body = values(form);
  const r = record
    ? await api.patch(`/admin/v1/records/${record.id}`, body)
    : await api.post("/admin/v1/records", body);
  outcome(slot, r, record ? "Saved" : "Added");
  if (r.ok) { view.reload(); if (!record) form.reset(); }
}

async function remove(record) {
  if (!window.confirm(`Delete the ${record.kind} at ${record.company}?`)) return;
  const r = await api.del(`/admin/v1/records/${record.id}`);
  toast(r.ok ? "Record deleted" : `Delete failed: ${r.error?.code}`);
  if (r.ok) { view.reload(); edit(null); }
}

let batchText;
let batchBox;

// "Add to batch" appends the form as one JSON record; hand edits to the JSON are kept.
function toBatch(form) {
  if (!form.reportValidity()) return;
  let records = [];
  try { records = batchText.value.trim() ? JSON.parse(batchText.value) : []; } catch { toast("The batch JSON is not valid; fix it first"); return; }
  records = Array.isArray(records) ? records : [records];
  records.push(values(form));
  batchText.value = JSON.stringify(records, null, 2);
  batchBox.open = true;
  toast(`${records.length} record(s) in the batch`);
  form.reset();
}

function batch() {
  batchText = h("textarea", { rows: 6, placeholder: '[{"kind": "application", "company": "Acme", "role": "AI Engineer", "stage": "applied", "date": "2026-10-22"}]' });
  const slot = h("div");
  const form = h("form", { class: "form", on: { submit: async (e) => {
    e.preventDefault();
    let records;
    try { records = JSON.parse(batchText.value); } catch { outcome(slot, { ok: false, status: 422, error: { code: "INVALID_JSON" }, message: "That is not valid JSON" }); return; }
    const r = await api.post("/admin/v1/records/batch", { records: Array.isArray(records) ? records : [records] });
    outcome(slot, r, r.ok ? `Added ${r.data.length}` : "");
    if (r.ok) { view.reload(); batchText.value = ""; }
  } } }, batchText, h("button", { class: "btn", type: "submit", text: "Add batch" }), slot);
  batchBox = h("details", { class: "batch" }, h("summary", { text: "Add a batch (JSON)" }), form);
  return batchBox;
}
