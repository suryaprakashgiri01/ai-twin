// Memories: instructions, preferences and aliases the owner agents keep in mind. Pending ones
// were suggested from conversations; accept, edit, archive or forget them.

import { request } from "../api.js";
import { badge, clear, h, select, toast } from "../dom.js";
import { field, head, listView, outcome, values } from "./common.js";

const KINDS = ["instruction", "preference", "alias"];
let view;
let editor;

export function mount(root) {
  editor = h("div", { class: "card" });
  view = listView({
    path: "/admin/v1/memories",
    sort: ["updatedAt", "desc"],
    filters: { status: ["pending", "active", "archived"], kind: KINDS, tier: ["private", "confidential"] },
    columns: [
      ["Memory", (m) => m.text],
      ["Kind", (m) => m.kind],
      ["Status", (m) => badge(m.status, m.status === "active" ? "ok" : m.status === "pending" ? "warn" : "")],
      ["Tier", (m) => m.tier],
      ["Uses", (m) => m.uses],
    ],
    onRow: (m) => edit(m),
  });
  root.append(head("Memories", "What the agents remember about how you like answers. Pending ones need your accept."),
    h("div", { class: "split" }, view.el, editor));
  edit(null);
  view.reload();
}

export function shown() { view?.reload(); }

function edit(memory) {
  const slot = h("div");
  const text = h("textarea", { name: "text", rows: 3, required: true, text: memory?.text || "" });
  const form = h("form", { class: "form", on: { submit: async (e) => {
    e.preventDefault();
    const body = values(form);
    const r = memory
      ? await request("PATCH", `/admin/v1/memories/${memory.id}`, { body })
      : await request("POST", "/admin/v1/memories", { body });
    outcome(slot, r, memory ? "Saved" : "Remembered");
    if (r.ok) { view.reload(); if (!memory) form.reset(); }
  } } },
  h("div", { class: "card-title" }, h("strong", { text: memory ? "Edit memory" : "Remember something" }),
    memory ? badge(memory.status) : null,
    memory ? h("button", { class: "btn btn-sm", type: "button", text: "New instead", on: { click: () => edit(null) } }) : null),
  field("Text", text),
  h("div", { class: "row" },
    field("Kind", select(KINDS, memory?.kind || "instruction", { name: "kind" })),
    field("Tier", select(["private", "confidential"], memory?.tier || "private", { name: "tier" }))),
  h("div", { class: "row" },
    h("button", { class: "btn btn-primary", type: "submit", text: memory ? "Save" : "Remember" }),
    memory?.status === "pending" ? status(memory, "active", "Accept") : null,
    memory && memory.status !== "archived" ? status(memory, "archived", "Archive") : null,
    memory ? h("button", { class: "btn btn-danger", type: "button", text: "Forget", on: { click: () => forget(memory) } }) : null),
  slot);
  clear(editor, form);
}

function status(memory, value, label) {
  return h("button", { class: "btn", type: "button", text: label, on: { click: async () => {
    const r = await request("PATCH", `/admin/v1/memories/${memory.id}`, { body: { status: value } });
    toast(r.ok ? `${label}d` : `${label} failed: ${r.error?.code}`);
    if (r.ok) { view.reload(); edit(r.data); }
  } } });
}

async function forget(memory) {
  if (!window.confirm("Forget this memory for good?")) return;
  const r = await request("DELETE", `/admin/v1/memories/${memory.id}`);
  toast(r.ok ? "Forgotten" : `Delete failed: ${r.error?.code}`);
  if (r.ok) { view.reload(); edit(null); }
}
