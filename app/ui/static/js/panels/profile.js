// Profile: availability, notice period, preferred roles, locations, reply time and the resume.

import { request } from "../api.js";
import { clear, errorLine, h, kv, time } from "../dom.js";
import { field, head, list, outcome, values } from "./common.js";

let card;

export function mount(root) {
  card = h("div", { class: "card" });
  root.append(head("Profile", "What the public assistant may say about your availability; the resume it offers."), card);
  load();
}

async function load() {
  const r = await request("GET", "/admin/v1/profile");
  if (!r.ok && r.status !== 404) { clear(card, errorLine(r)); return; }
  const p = r.data || { availability: "", preferredRoles: [], locations: [] };
  const slot = h("div");
  const form = h("form", { class: "form", on: { submit: async (e) => {
    e.preventDefault();
    const v = values(form);
    const body = {
      availability: v.availability || "",
      noticePeriod: v.noticePeriod ?? null,
      preferredRoles: list(v.preferredRoles),
      locations: list(v.locations),
      replyTime: v.replyTime ?? null,
      resumeDocumentId: v.resumeDocumentId ?? null,
    };
    const saved = await request("PUT", "/admin/v1/profile", { body });
    outcome(slot, saved, "Profile saved");
  } } },
  field("Availability", h("input", { name: "availability", value: p.availability || "", placeholder: "open to offers" })),
  h("div", { class: "row" },
    field("Notice period", h("input", { name: "noticePeriod", value: p.noticePeriod || "" })),
    field("Reply time", h("input", { name: "replyTime", value: p.replyTime || "", placeholder: "within 2 days" }))),
  field("Preferred roles", h("input", { name: "preferredRoles", value: (p.preferredRoles || []).join(", ") }), "comma separated"),
  field("Locations", h("input", { name: "locations", value: (p.locations || []).join(", ") }), "comma separated"),
  field("Resume document id", h("input", { name: "resumeDocumentId", value: p.resumeDocumentId || "" }), "an approved public resume document (see Review)"),
  h("button", { class: "btn btn-primary", type: "submit", text: "Save profile" }),
  slot);
  clear(card, form, p.updatedAt ? kv([["Updated", time(p.updatedAt)], ["Resume file", p.resumeBlob ? `${p.resumeBlob.filename || "stored"}` : "—"]]) : null);
}
