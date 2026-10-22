// Models & agents: each owner agent's model settings (pin or auto, applied at once), and the
// model inventory with its overrides (enable or disable, lower a tier cap).

import { request, rows } from "../api.js";
import { badge, busy, clear, errorLine, h, select, table, toast } from "../dom.js";
import { field, head } from "./common.js";

const PREFER = ["", "fast", "quality", "paid", "local", "local-quality", "local-max"];
const NEED = ["", "simple", "moderate", "complex"];
let agentsCard;
let modelsCard;
let inventory = [];

export function mount(root) {
  agentsCard = h("div", { class: "card" });
  modelsCard = h("div", { class: "card" });
  root.append(head("Models & agents", "Pin a model per agent or leave it on auto. Confidential steps only ever use local models."),
    agentsCard, modelsCard,
    h("p", { class: "muted small" }, "Check every cloud model id against its provider: ", h("code", { text: "make models-check" }),
      " (Windows: ", h("code", { text: "./scripts/dev.ps1 models-check" }), ")."));
  load();
}

export function shown() { load(); }

const TONE = { active: "ok", disabled: "", "no key": "warn", "not pulled": "warn", unreachable: "err", missing: "err" };
const LABEL = { "not pulled": "needs install", "no key": "inactive · no key" };

async function load(verify = false) {
  const [models, agents] = await Promise.all([
    request("GET", "/admin/v1/models", { query: verify ? { verify: true } : {} }),
    request("GET", "/admin/v1/agents"),
  ]);
  inventory = rows(models);
  renderAgents(agents);
  renderModels(models);
}

function ids(filter) {
  return [["auto", "auto"], ...inventory.filter(filter).map((m) => [m.id, `${m.id}${m.status === "active" ? "" : ` (${LABEL[m.status] || m.status})`}`])];
}

function renderAgents(r) {
  if (!r.ok) { clear(agentsCard, errorLine(r)); return; }
  clear(agentsCard, h("div", { class: "card-title" }, h("strong", { text: "Agents" })),
    h("div", { class: "agents" }, rows(r).map(agentRow)));
}

function agentRow(a) {
  const model = select(ids((m) => m.roles.includes("generator")), a.model);
  const prefer = select(PREFER.map((p) => [p, p || "—"]), a.prefer || "");
  const confidential = select(ids((m) => m.local && m.roles.includes("generator")), a.confidentialModel);
  const confidentialPrefer = select(PREFER.map((p) => [p, p || "—"]), a.confidentialPrefer || "");
  const need = select(NEED.map((n) => [n, n || "profile each request"]), a.need || "");
  const save = h("button", { class: "btn btn-primary btn-sm", type: "button", text: "Save" });
  const reset = h("button", { class: "btn btn-sm", type: "button", text: "Reset to file", disabled: a.source !== "override" });
  save.addEventListener("click", () => busy(save, async () => {
    const body = { model: model.value, confidentialModel: confidential.value };
    if (prefer.value) body.prefer = prefer.value;
    if (confidentialPrefer.value) body.confidentialPrefer = confidentialPrefer.value;
    if (need.value) body.need = need.value;
    const r = await request("PUT", `/admin/v1/agents/${a.name}`, { body });
    toast(r.ok ? `${a.name}: saved, the next call uses it` : `${a.name}: ${r.error?.code} ${r.message}`);
    if (r.ok) load();
  }));
  reset.addEventListener("click", () => busy(reset, async () => {
    const r = await request("DELETE", `/admin/v1/agents/${a.name}`);
    toast(r.ok ? `${a.name}: back to config/agents.yaml` : `Reset failed: ${r.error?.code}`);
    if (r.ok) load();
  }));
  return h("div", { class: "agent-row" },
    h("div", { class: "card-title" }, h("strong", { text: a.name }), badge(a.source, a.source === "override" ? "warn" : "info"),
      h("span", { class: "muted small", text: a.description }), h("code", { class: "muted", text: a.endpoint })),
    h("div", { class: "row" }, field("Model", model), field("Prefer", prefer), field("Confidential model", confidential),
      field("Confidential prefer", confidentialPrefer), field("Need", need), save, reset));
}

function renderModels(r) {
  if (!r.ok) { clear(modelsCard, errorLine(r)); return; }
  const all = rows(r);
  const active = all.filter((m) => m.status === "active").length;
  const verify = h("button", { class: "btn btn-sm", type: "button", text: "Check providers", title: "Ask each cloud provider whether its model ids are still served (like make models-check)" });
  verify.addEventListener("click", () => busy(verify, () => load(true)));
  clear(modelsCard, h("div", { class: "card-title" }, h("strong", { text: "Inventory" }),
    h("span", { class: "muted small", text: `${active} of ${all.length} active · overrides apply to the next call; a tier cap can only be lowered` }),
    h("span", { class: "spacer" }), verify),
  table(all, [
    ["Model", (m) => h("span", { class: "mono", text: m.id })],
    ["Status", (m) => h("div", {}, badge(LABEL[m.status] || m.status, TONE[m.status]), m.hint ? h("div", { class: "muted small", text: m.hint }) : null)],
    ["Provider", (m) => (m.local ? `${m.provider} (local)` : m.provider)],
    ["Roles", (m) => m.roles.join(", ")],
    ["Quality", (m) => m.quality],
    ["Capabilities", (m) => m.capabilities.join(", ")],
    ["Enabled", (m) => toggle(m)],
    ["Max tier", (m) => tierPick(m)],
  ]));
}

async function override(m, body) {
  const r = await request("PUT", `/admin/v1/models/overrides/${encodeURIComponent(m.id)}`, { body });
  toast(r.ok ? `${m.id}: override saved` : `${m.id}: ${r.error?.code} ${r.message}`);
  load();
}

function toggle(m) {
  return h("input", { type: "checkbox", checked: m.enabled, "aria-label": `Enable ${m.id}`, on: { change: (e) => override(m, { enabled: e.target.checked, maxTier: m.maxTier === "restricted" ? undefined : m.maxTier }) } });
}

function tierPick(m) {
  return select(["public", "private", "confidential"], m.maxTier, { "aria-label": `Max tier for ${m.id}`, on: { change: (e) => override(m, { enabled: m.enabled, maxTier: e.target.value }) } });
}
