import { state } from "./api.js";
import { login, logout, roleBadge } from "./auth.js";
import { clear, h } from "./dom.js";
import { refresh, setupSidebar } from "./sidebar.js";
import { setupTheme } from "./theme.js";
import * as chat from "./panels/chat.js";
import * as ingest from "./panels/ingest.js";
import * as leads from "./panels/leads.js";
import * as memories from "./panels/memories.js";
import * as models from "./panels/models.js";
import * as opportunity from "./panels/opportunity.js";
import * as profile from "./panels/profile.js";
import * as records from "./panels/records.js";
import * as review from "./panels/review.js";
import * as security from "./panels/security.js";

// Tabs in order, with the roles that see them. The API refuses anything else anyway.
const OWNER = ["owner"];
const TABS = [
  { name: "chat", label: "Chat", panel: chat, roles: ["owner", "public"] },
  { name: "ingest", label: "Ingest", panel: ingest, roles: OWNER },
  { name: "review", label: "Review", panel: review, roles: OWNER },
  { name: "records", label: "Records", panel: records, roles: OWNER },
  { name: "profile", label: "Profile", panel: profile, roles: OWNER },
  { name: "memories", label: "Memories", panel: memories, roles: OWNER },
  { name: "models", label: "Models", panel: models, roles: OWNER },
  { name: "leads", label: "Leads", panel: leads, roles: OWNER },
  { name: "opportunity", label: "Opportunity", panel: opportunity, roles: ["owner", "leads"] },
  { name: "security", label: "Security", panel: security, roles: OWNER },
];
const mounted = new Set();

function visible() {
  return TABS.filter((t) => t.roles.includes(state.me?.role));
}

function show(name) {
  const tab = visible().find((t) => t.name === name) || visible()[0];
  for (const button of document.querySelectorAll("#tabs [role=tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab.name));
  }
  for (const panel of document.querySelectorAll("section[data-panel]")) panel.hidden = panel.dataset.panel !== tab.name;
  document.getElementById("sidebar")?.classList.toggle("only-chat", tab.name !== "chat");
  if (!mounted.has(tab.name)) {
    tab.panel.mount(document.getElementById(`panel-${tab.name}`));
    mounted.add(tab.name);
  } else tab.panel.shown?.();
  history.replaceState(null, "", `#${tab.name}`);
}

async function boot() {
  setupTheme();
  const me = await login(document.getElementById("login"));
  document.getElementById("app").hidden = false;
  clear(document.getElementById("role-badge"), roleBadge(me));
  document.getElementById("logout").addEventListener("click", logout);
  document.getElementById("side-toggle").addEventListener("click", () => {
    const narrow = window.matchMedia("(max-width: 900px)").matches;
    document.body.classList.toggle(narrow ? "side-open" : "side-closed");
  });

  const tabs = document.getElementById("tabs");
  const panels = document.getElementById("panels");
  for (const t of visible()) {
    tabs.append(h("button", { role: "tab", type: "button", dataset: { tab: t.name }, text: t.label, on: { click: () => show(t.name) } }));
    panels.append(h("section", { id: `panel-${t.name}`, class: "panel", dataset: { panel: t.name }, hidden: true }));
  }
  tabs.hidden = visible().length < 2;

  const hasChat = visible().some((t) => t.name === "chat");
  if (hasChat) {
    setupSidebar(document.getElementById("sidebar"), {
      open: (c) => { show("chat"); chat.open(c); },
      fresh: () => { show("chat"); chat.fresh(); },
    });
  } else document.getElementById("sidebar").remove();
  show(location.hash.slice(1) || visible()[0].name);
  if (hasChat) refresh();
}

boot();
