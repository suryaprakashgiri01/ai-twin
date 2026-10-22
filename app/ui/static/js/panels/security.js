// Security: guard events on the public surface (no visitor text is ever stored) and alerting.

import { api } from "../api.js";
import { badge, busy, clear, errorLine, h, kv, time, toast } from "../dom.js";
import { head, listView } from "./common.js";

let view;
let alerts;

export function mount(root) {
  alerts = h("div", { class: "card" });
  view = listView({
    path: "/admin/v1/security/events",
    sort: ["createdAt", "desc"],
    filters: { severity: ["low", "medium", "high", "critical"] },
    columns: [["When", (e) => time(e.createdAt)], ["Kind", (e) => e.kind],
      ["Severity", (e) => badge(e.severity, e.severity === "critical" || e.severity === "high" ? "err" : e.severity === "medium" ? "warn" : "")],
      ["Rules", (e) => e.rules.join(", ")], ["Surface", (e) => e.surface], ["Alerted", (e) => (e.alertedAt ? time(e.alertedAt) : "—")]],
    emptyText: "No security events.",
  });
  root.append(head("Security", "What the public guards blocked or flagged, and how alerts reach you."), alerts, view.el);
  loadAlerts();
  view.reload();
}

export function shown() { view?.reload(); loadAlerts(); }

async function loadAlerts() {
  const r = await api.get("/admin/v1/alerts/status");
  if (!r.ok) { clear(alerts, errorLine(r)); return; }
  const a = r.data;
  const test = h("button", { class: "btn btn-sm", type: "button", text: "Send a test alert", disabled: !a.smtpConfigured });
  test.addEventListener("click", () => busy(test, async () => {
    if (!window.confirm("Send one test alert email to your alert address?")) return;
    const sent = await api.post("/admin/v1/alerts/test");
    toast(sent.ok && sent.data.sent ? "Test alert sent" : `Not sent: ${sent.error?.code || "check SMTP"}`);
  }));
  clear(alerts,
    h("div", { class: "card-title" }, h("strong", { text: "Alerts" }), badge(a.smtpConfigured ? "SMTP configured" : "SMTP not configured", a.smtpConfigured ? "ok" : "warn"), h("span", { class: "spacer" }), test),
    kv([["Cooldown", `${a.cooldownMinutes} min`], ["Digest", `${a.digestHour}:00 ${a.timezone}`], ["Last digest", a.lastDigest ? time(a.lastDigest) : "—"],
      ["Policies", a.policies.map((p) => `${p.name} (${p.mode}${p.cooldownRemainingSeconds ? `, cooling ${Math.ceil(p.cooldownRemainingSeconds / 60)} min` : ""})`).join(", ") || "—"]]));
}
