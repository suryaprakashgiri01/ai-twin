// Profile: availability, notice period, preferred roles, locations, reply time and the resume.

import { api } from "../api.js";
import { clear, errorLine, h, kv, select, time } from "../dom.js";
import { field, head, list, outcome, values } from "./common.js";

let card;
const AVAILABILITY = ["Actively looking", "Open to offers", "Not looking", "Available from a date (say when)"];

// A dropdown of the usual answers, with "Other…" for your own words.
function availabilityField(stored) {
  const match = AVAILABILITY.find((a) => a.toLowerCase() === stored.trim().toLowerCase());
  const current = match || stored;
  const known = Boolean(match);
  const pick = select([...AVAILABILITY.map((a) => [a, a]), ["__other", "Other…"]], known || !current ? current || AVAILABILITY[1] : "__other");
  const other = h("input", { placeholder: "In your own words, e.g. available from January", value: known ? "" : current, hidden: known || !current });
  const hidden = h("input", { type: "hidden", name: "availability", value: current || AVAILABILITY[1] });
  const sync = () => {
    other.hidden = pick.value !== "__other";
    hidden.value = pick.value === "__other" ? other.value.trim() : pick.value;
  };
  pick.addEventListener("change", sync);
  other.addEventListener("input", sync);
  sync();
  return field("Availability", h("div", { class: "row" }, pick, other, hidden), "What the public assistant says when asked if you are available");
}

export function mount(root) {
  card = h("div", { class: "card" });
  root.append(head("Profile", "What the public assistant may say about your availability; the resume it offers."), card);
  load();
}

async function load() {
  const r = await api.get("/admin/v1/profile");
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
    const saved = await api.put("/admin/v1/profile", body);
    outcome(slot, saved, "Profile saved");
  } } },
  availabilityField(p.availability || ""),
  h("div", { class: "row" },
    field("Notice period", h("input", { name: "noticePeriod", value: p.noticePeriod || "" })),
    field("Reply time", h("input", { name: "replyTime", value: p.replyTime || "", placeholder: "within 2 days" }))),
  field("Preferred roles", h("input", { name: "preferredRoles", value: (p.preferredRoles || []).join(", "), placeholder: "AI Engineer, Backend Engineer" })),
  field("Locations", h("input", { name: "locations", value: (p.locations || []).join(", "), placeholder: "Remote, Bengaluru" })),
  field("Resume document id", h("input", { name: "resumeDocumentId", value: p.resumeDocumentId || "", placeholder: "Document id from Review" }), "An approved, public resume document; visitors can download it"),
  h("button", { class: "btn btn-primary", type: "submit", text: "Save profile" }),
  slot);
  clear(card, form, p.updatedAt ? kv([["Updated", time(p.updatedAt)], ["Resume file", p.resumeBlob ? `${p.resumeBlob.filename || "stored"}` : "—"]]) : null);
}
