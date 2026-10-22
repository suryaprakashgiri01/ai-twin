// Conversation history, grouped like a chat app: Today, Yesterday, Previous 7 days, Previous
// 30 days, then by month. Open, rename (the title) and soft delete, plus search.

import { api, isOwner, rows } from "./api.js";
import { clear, h, toast } from "./dom.js";

const PAGE = 200;
let all = [];
let filter = "";
let activeId = null;
let handlers = { open: () => {}, fresh: () => {} };
let listEl;

export function tagOf(c) {
  return c.channel === "public" ? "public" : `owner·${c.mode || "ask"}`;
}

export function titleOf(c) {
  if (c.description || c.name) return c.description || c.name;
  // Visitor chats are never titled: name them by when they started.
  return c.channel === "public" ? `Visitor chat · ${new Date(c.createdAt).toLocaleString()}` : "New conversation";
}

function base() {
  return isOwner() ? "/admin/v1/conversations" : "/v1/conversations";
}

export function setupSidebar(root, { open, fresh }) {
  handlers = { open, fresh };
  const search = h("input", { type: "search", placeholder: "Search chats", "aria-label": "Search chats",
    on: { input: (e) => { filter = e.target.value.trim().toLowerCase(); render(); } } });
  listEl = h("nav", { class: "history", "aria-label": "Conversation history" });
  clear(root,
    h("div", { class: "side-head" },
      h("button", { class: "btn btn-primary new-chat", type: "button", text: "+ New chat", on: { click: () => handlers.fresh() } }),
      search),
    listEl);
}

export async function refresh() {
  const query = { pageSize: PAGE, sortColumn: "updatedAt", sortOrder: "desc" };
  if (isOwner()) query.channel = "all";
  const r = await api.get(base(), query);
  if (!r.ok) {
    clear(listEl, h("div", { class: "muted small pad", text: `History unavailable (${r.error?.code})` }));
    return;
  }
  all = rows(r);
  render();
}

export function setActive(id) {
  activeId = id;
  for (const el of listEl.querySelectorAll(".conv")) el.classList.toggle("active", el.dataset.id === id);
}

function bucket(iso, now) {
  const d = new Date(iso);
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = 86_400_000;
  if (d >= start) return "Today";
  if (d >= new Date(start - day)) return "Yesterday";
  if (d >= new Date(start - 7 * day)) return "Previous 7 days";
  if (d >= new Date(start - 30 * day)) return "Previous 30 days";
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function render() {
  const now = new Date();
  const shown = all.filter((c) => !filter || `${titleOf(c)} ${c.name || ""} ${tagOf(c)}`.toLowerCase().includes(filter));
  if (!shown.length) {
    clear(listEl, h("div", { class: "muted small pad", text: filter ? "No chat matches." : "No conversations yet." }));
    return;
  }
  const groups = new Map();
  for (const c of shown) {
    const name = bucket(c.updatedAt, now);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(c);
  }
  clear(listEl, [...groups].map(([name, items]) => h("section", { class: "group" },
    h("h4", { text: name }),
    items.map(row))));
  setActive(activeId);
}

function row(c) {
  const readOnly = isOwner() && c.channel === "public";
  const title = h("span", { class: "conv-title", text: titleOf(c), title: c.name || "" });
  const el = h("div", { class: "conv", dataset: { id: c.id } },
    h("button", { class: "conv-open", type: "button", "aria-label": `${titleOf(c)} (${tagOf(c)})`, on: { click: () => handlers.open(c) } },
      title, h("span", { class: `tag tag-${c.channel}`, text: tagOf(c) })),
    readOnly ? null : h("div", { class: "conv-actions" },
      h("button", { class: "icon", type: "button", title: "Rename", "aria-label": "Rename", text: "✎", on: { click: () => rename(c, el, title) } }),
      h("button", { class: "icon danger", type: "button", title: "Delete", "aria-label": "Delete", text: "🗑", on: { click: () => remove(c) } })));
  return el;
}

function rename(c, el, title) {
  const input = h("input", { class: "rename", value: titleOf(c), maxLength: 100 });
  title.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const text = input.value.trim();
    if (save && text && text !== titleOf(c)) {
      const r = await api.patch(`${base()}/${c.id}`, { description: text });
      if (r.ok) { c.description = text; toast("Renamed"); } else toast(`Rename failed: ${r.error?.code}`);
    }
    render();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

async function remove(c) {
  if (!window.confirm(`Delete “${titleOf(c)}”? It disappears from history and cannot be continued.`)) return;
  const r = await api.del(`${base()}/${c.id}`);
  if (!r.ok) { toast(`Delete failed: ${r.error?.code}`); return; }
  all = all.filter((x) => x.id !== c.id);
  render();
  toast("Conversation deleted");
  if (activeId === c.id) handlers.fresh();
}
