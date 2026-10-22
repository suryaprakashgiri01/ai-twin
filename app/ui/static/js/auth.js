// The login screen: any API key, checked with GET /v1/me. The role decides what the console shows.

import { api, request, setKey, state } from "./api.js";
import { clear, h } from "./dom.js";

const ROLE_LABEL = { owner: "owner", public: "public", leads: "leads" };

/** Resolves with the /v1/me answer once a key works; shows the login form until then. */
export async function login(screen) {
  if (state.key) {
    const r = await api.get("/v1/me");
    if (r.ok) return remember(r.data);
    setKey("", false);
  }
  return new Promise((resolve) => renderForm(screen, resolve));
}

function remember(me) {
  state.me = me;
  return me;
}

function renderForm(screen, resolve, message = "") {
  const input = h("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: "atw_…", required: true, id: "login-key" });
  const keep = h("input", { type: "checkbox", id: "login-remember" });
  const error = h("div", { class: "error-line", text: message, hidden: !message });
  const submit = h("button", { class: "btn btn-primary", type: "submit", text: "Sign in" });
  const form = h("form", { class: "login-card", on: { submit: onSubmit } },
    h("div", { class: "brand" }, h("span", { class: "logo", "aria-hidden": "true" }),
      h("div", {}, h("div", { class: "brand-name", text: "ai-twin console" }),
        h("div", { class: "brand-sub", text: "Sign in with an API key" }))),
    h("label", { class: "field" }, h("span", { text: "X-API-Key" }), input),
    h("label", { class: "check" }, keep, h("span", { text: "Remember on this device (otherwise forgotten when the tab closes)" })),
    h("p", { class: "muted small", text: "The admin key opens every agent and tab. A public key opens the public assistant and its own chats only." }),
    error,
    submit);
  clear(screen, form);
  screen.hidden = false;
  input.focus();

  async function onSubmit(event) {
    event.preventDefault();
    submit.disabled = true;
    const key = input.value.trim();
    const r = await request("GET", "/v1/me", { key });
    submit.disabled = false;
    if (!r.ok) {
      error.textContent = r.status === 401 ? "That key was not accepted." : `${r.error?.code}: ${r.message}`;
      error.hidden = false;
      return;
    }
    setKey(key, keep.checked);
    screen.hidden = true;
    resolve(remember(r.data));
  }
}

export function roleBadge(me) {
  return h("span", { class: `role role-${me.role}`, title: `scopes: ${me.scopes.join(", ")}`, text: `${me.name} · ${ROLE_LABEL[me.role] || me.role}` });
}

export function logout() {
  setKey("", false);
  state.me = null;
  window.location.reload();
}
