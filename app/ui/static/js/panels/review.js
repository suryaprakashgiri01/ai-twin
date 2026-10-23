// Review: pending and quarantined documents. Open one to read its redacted text, approve,
// publish or unpublish, release a quarantined one, relabel it or delete it.

import { api } from "../api.js";
import { badge, busy, clear, errorLine, h, kv, select, time, toast } from "../dom.js";
import { CATEGORIES, TIERS, field, head, list, listView } from "./common.js";

let detail;
let view;

export function mount(root) {
  detail = h("div", { class: "card", hidden: true });
  view = listView({
    path: "/admin/v1/review",
    sort: ["createdAt", "desc"],
    filters: { reviewStatus: ["pending", "approved", "rejected", "quarantined"], tier: TIERS, category: CATEGORIES },
    defaults: { reviewStatus: "pending" },
    columns: [
      ["Title", (d) => d.title, "title"],
      ["Status", (d) => badge(d.reviewStatus, d.reviewStatus === "quarantined" ? "warn" : d.reviewStatus === "approved" ? "ok" : "")],
      ["Tier", (d) => d.tier, "tier"],
      ["Category", (d) => d.category, "category"],
      ["Published", (d) => (d.published ? "yes" : "—")],
      ["Created", (d) => time(d.createdAt), "createdAt"],
    ],
    onRow: (d) => open(d.id),
    autoSelect: true,
    emptyText: "The review queue is empty.",
  });
  const cards = h("button", { class: "btn", type: "button", text: "Write missing cards", title: "Overview summaries for documents that have none" });
  cards.addEventListener("click", () => busy(cards, async () => {
    const r = await api.post("/admin/v1/documents/cards", { missingOnly: true, limit: 50 });
    toast(r.ok ? `Cards: ${r.data.written} written, ${r.data.skipped} skipped of ${r.data.checked}` : `Cards failed: ${r.error?.code}`);
  }));
  root.append(head("Review", "Nothing reaches an answer, and nothing is public, before you approve it here.", cards), h("div", { class: "split" }, view.el, detail));
  view.reload();
}

export function shown() { view?.reload(); }

async function open(id) {
  const r = await api.get(`/admin/v1/documents/${id}`);
  detail.hidden = false;
  if (!r.ok) { clear(detail, errorLine(r)); return; }
  render(r.data);
}

function render(d) {
  const act = (label, path, body, tone = "") => {
    const b = h("button", { class: `btn btn-sm ${tone}`, type: "button", text: label });
    b.addEventListener("click", () => busy(b, async () => {
      const r = await api.post(`/admin/v1/documents/${d.id}/${path}`, body);
      toast(r.ok ? r.message || label : `${label} failed: ${r.error?.code} ${r.message}`);
      if (r.ok) { render(r.data); view.reload(); }
    }));
    return b;
  };
  const localOnly = h("input", { type: "checkbox", checked: true });
  const actions = h("div", { class: "row" },
    d.reviewStatus !== "approved" && d.reviewStatus !== "quarantined" ? act("Approve", "approve") : null,
    d.reviewStatus === "approved" && d.tier === "public" && !d.published ? act("Publish", "publish", undefined, "btn-primary") : null,
    d.published ? act("Unpublish", "unpublish") : null,
    d.reviewStatus === "quarantined" ? [act("Release", "release", { get localOnly() { return localOnly.checked; } }),
      h("label", { class: "check" }, localOnly, h("span", { text: "local models only" }))] : null,
    h("span", { class: "spacer" }),
    h("button", { class: "btn btn-sm btn-danger", type: "button", text: "Delete", on: { click: () => remove(d) } }));
  clear(detail,
    h("div", { class: "card-title" }, h("strong", { text: d.title }), badge(d.reviewStatus), badge(d.tier, d.tier === "public" ? "ok" : "info"),
      d.published ? badge("published", "ok") : null, d.localOnly ? badge("local only", "warn") : null),
    kv([["Category", d.category], ["Tags", d.tags.join(", ") || "—"], ["Occurred", d.occurredAt || "—"], ["Created", time(d.createdAt)],
      ["Quarantine rules", d.quarantineRules.join(", ") || "—"], ["Redactions", Object.entries(d.redactionReport || {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "—"]]),
    actions,
    relabel(d),
    d.card ? h("details", {}, h("summary", { text: "Card" }), h("p", { class: "preview", text: d.card })) : null,
    h("details", { open: true }, h("summary", { text: "Redacted text" }), h("pre", { class: "preview", text: d.text || "(no text)" })));
}

function relabel(d) {
  const title = h("input", { value: d.title });
  const category = select(CATEGORIES, d.category);
  const tier = select(TIERS, d.tier);
  const tags = h("input", { value: d.tags.join(", ") });
  const save = h("button", { class: "btn btn-sm", type: "submit", text: "Save labels" });
  return h("form", { class: "row relabel", on: { submit: async (e) => {
    e.preventDefault();
    const r = await api.patch(`/admin/v1/documents/${d.id}`, { title: title.value.trim(), category: category.value, tier: tier.value, tags: list(tags.value) });
    toast(r.ok ? "Labels saved" : `Save failed: ${r.error?.code} ${r.message}`);
    if (r.ok) { render(r.data); view.reload(); }
  } } }, field("Title", title), field("Category", category), field("Tier", tier), field("Tags", tags), save);
}

async function remove(d) {
  if (!window.confirm(`Delete “${d.title}”? Its vectors are removed from every collection.`)) return;
  const r = await api.del(`/admin/v1/documents/${d.id}`);
  toast(r.ok ? "Document deleted" : `Delete failed: ${r.error?.code}`);
  if (r.ok) { detail.hidden = true; view.reload(); }
}
