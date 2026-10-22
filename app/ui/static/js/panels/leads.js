// Leads: opportunities shared through the portfolio, with their triage. Open one, set its status.

import { api } from "../api.js";
import { badge, clear, h, kv, select, time, toast } from "../dom.js";
import { head, listView } from "./common.js";

const STATUS = ["new", "reviewed", "replied", "archived"];
let view;
let detail;

export function mount(root) {
  detail = h("div", { class: "card", hidden: true });
  view = listView({
    path: "/admin/v1/leads",
    sort: ["createdAt", "desc"],
    filters: { status: STATUS },
    columns: [["Received", (l) => time(l.createdAt)], ["Name", (l) => l.name], ["Company", (l) => l.company], ["Role", (l) => l.role],
      ["Triage", (l) => l.triage || "—"], ["Status", (l) => badge(l.status, l.status === "new" ? "warn" : "")]],
    onRow: open,
    emptyText: "No leads yet.",
  });
  root.append(head("Leads", "Opportunities visitors shared. Contact details stay here; nothing is sent from this tab."),
    h("div", { class: "split" }, view.el, detail));
  view.reload();
}

export function shown() { view?.reload(); }

function open(l) {
  detail.hidden = false;
  const status = select(STATUS, l.status, { on: { change: async (e) => {
    const r = await api.patch(`/admin/v1/leads/${l.id}`, { status: e.target.value });
    toast(r.ok ? `Marked ${e.target.value}` : `Update failed: ${r.error?.code}`);
    if (r.ok) view.reload();
  } } });
  clear(detail,
    h("div", { class: "card-title" }, h("strong", { text: `${l.name} · ${l.company || "—"}` }), status),
    kv([["Email", l.email], ["Role", l.role], ["Location", l.location], ["Type", l.employmentType], ["Link", l.link],
      ["Triage", l.triage ? `${l.triage} (${Math.round((l.triageConfidence || 0) * 100)}%)` : "—"], ["Fit", l.fitNote], ["Duplicates", l.duplicates],
      ["Emailed", l.emailedAt ? time(l.emailedAt) : "—"], ["Source", l.source]]),
    l.summary ? h("p", { class: "muted", text: l.summary }) : null,
    h("pre", { class: "preview", text: l.message }));
}
