// Opportunity: the portfolio's contact form (POST /v1/opportunities), for a `leads` key or the owner.

import { api } from "../api.js";
import { h } from "../dom.js";
import { field, head, outcome, values } from "./common.js";

export function mount(root) {
  const slot = h("div");
  const form = h("form", { class: "card form", on: { submit: async (e) => {
    e.preventDefault();
    const r = await api.post("/v1/opportunities", values(form));
    outcome(slot, r, "Shared. It shows up under Leads.");
    if (r.ok) form.reset();
  } } },
  h("div", { class: "row" }, field("Name", h("input", { name: "name", required: true })), field("Email", h("input", { name: "email", type: "email", required: true }))),
  h("div", { class: "row" }, field("Company", h("input", { name: "company", required: true })), field("Role", h("input", { name: "role", required: true }))),
  h("div", { class: "row" }, field("Location", h("input", { name: "location" })), field("Employment type", h("input", { name: "employmentType", placeholder: "full-time, contract…" })),
    field("Link", h("input", { name: "link", type: "url" }))),
  field("Message", h("textarea", { name: "message", rows: 5, required: true })),
  h("button", { class: "btn btn-primary", type: "submit", text: "Share opportunity" }),
  slot);
  root.append(head("Opportunity", "The form visitors use on the portfolio. Turnstile is checked when it is configured."), form);
}
