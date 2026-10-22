import { isOwner } from "./api.js";
import { login, logout, roleBadge } from "./auth.js";
import { clear, h } from "./dom.js";
import { refresh, setupSidebar } from "./sidebar.js";
import { setupTheme } from "./theme.js";
import * as chat from "./panels/chat.js";

// Tabs in order. `owner: true` tabs need the admin key; the API refuses them anyway.
const TABS = [
  { name: "chat", label: "Chat", panel: chat, owner: false },
];
const mounted = new Set();

function visible() {
  return TABS.filter((t) => isOwner() || !t.owner);
}

function show(name) {
  const tab = visible().find((t) => t.name === name) || visible()[0];
  for (const button of document.querySelectorAll("#tabs [role=tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab.name));
  }
  for (const panel of document.querySelectorAll("section[data-panel]")) panel.hidden = panel.dataset.panel !== tab.name;
  document.getElementById("sidebar").classList.toggle("only-chat", tab.name !== "chat");
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

  setupSidebar(document.getElementById("sidebar"), {
    open: (c) => { show("chat"); chat.open(c); },
    fresh: () => { show("chat"); chat.fresh(); },
  });
  show(location.hash.slice(1) || "chat");
  refresh();
}

boot();
