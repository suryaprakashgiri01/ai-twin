// Line icons (24×24, stroke), built with createElementNS: no HTML strings, CSP-safe.

const NS = "http://www.w3.org/2000/svg";

// Each icon is a list of SVG elements: [tag, attributes].
const ICONS = {
  send: [["path", { d: "M12 19V5" }], ["path", { d: "m5 12 7-7 7 7" }]],
  stop: [["rect", { x: 6, y: 6, width: 12, height: 12, rx: 2 }]],
  refresh: [["path", { d: "M21 12a9 9 0 1 1-2.64-6.36" }], ["path", { d: "M21 3v6h-6" }]],
  edit: [["path", { d: "M12 20h9" }], ["path", { d: "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" }]],
  trash: [["path", { d: "M3 6h18" }], ["path", { d: "M8 6V4h8v2" }], ["path", { d: "M19 6l-1 14H6L5 6" }], ["path", { d: "M10 11v6M14 11v6" }]],
  github: [["path", { d: "M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21" }]],
  file: [["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" }], ["path", { d: "M14 2v6h6M8 13h8M8 17h5" }]],
  briefcase: [["rect", { x: 2, y: 7, width: 20, height: 14, rx: 2 }], ["path", { d: "M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2M2 13h20" }]],
  user: [["circle", { cx: 12, cy: 8, r: 4 }], ["path", { d: "M4 21a8 8 0 0 1 16 0" }]],
  globe: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20" }]],
  link: [["path", { d: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" }], ["path", { d: "M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" }]],
  external: [["path", { d: "M15 3h6v6M10 14 21 3" }], ["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }]],
  x: [["path", { d: "M18 6 6 18M6 6l12 12" }]],
  upload: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }], ["path", { d: "m17 8-5-5-5 5M12 3v12" }]],
  down: [["path", { d: "M12 5v14" }], ["path", { d: "m19 12-7 7-7-7" }]],
  bell: [["path", { d: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" }], ["path", { d: "M10.3 21a1.9 1.9 0 0 0 3.4 0" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 10 }], ["path", { d: "M12 6v6l4 2" }]],
  hash: [["path", { d: "M4 9h16M4 15h16M10 3 8 21M16 3l-2 18" }]],
  cpu: [["rect", { x: 5, y: 5, width: 14, height: 14, rx: 2 }], ["path", { d: "M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" }]],
  shield: [["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10" }]],
  route: [["circle", { cx: 6, cy: 19, r: 3 }], ["circle", { cx: 18, cy: 5, r: 3 }], ["path", { d: "M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15" }]],
  mail: [["rect", { x: 2, y: 4, width: 20, height: 16, rx: 2 }], ["path", { d: "m22 7-10 6L2 7" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  first: [["path", { d: "m11 17-5-5 5-5M18 17l-5-5 5-5" }]],
  prev: [["path", { d: "m15 18-6-6 6-6" }]],
  next: [["path", { d: "m9 18 6-6-6-6" }]],
  last: [["path", { d: "m13 17 5-5-5-5M6 17l5-5-5-5" }]],
  star: [["path", { d: "m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1Z" }]],
  sparkles: [["path", { d: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z" }], ["path", { d: "M19 17v4M17 19h4" }]],
  alert: [["path", { d: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0" }], ["path", { d: "M12 9v4M12 17h.01" }]],
  check: [["path", { d: "M20 6 9 17l-5-5" }]],
  search: [["circle", { cx: 11, cy: 11, r: 8 }], ["path", { d: "m21 21-4.3-4.3" }]],
  sort: [["path", { d: "m7 15 5 5 5-5M7 9l5-5 5 5" }]],
  asc: [["path", { d: "m7 14 5-5 5 5" }]],
  desc: [["path", { d: "m7 10 5 5 5-5" }]],
  phone: [["path", { d: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2Z" }]],
  lock: [["rect", { x: 4, y: 11, width: 16, height: 10, rx: 2 }], ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }]],
  book: [["path", { d: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5Z" }], ["path", { d: "M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5" }]],
};

/** An inline SVG icon. Unknown names render an empty box rather than failing. */
export function icon(name, { size = 16, title, cls = "" } = {}) {
  const svg = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", class: `icon-svg ${cls}`.trim(), "aria-hidden": title ? "false" : "true" })) {
    svg.setAttribute(k, String(v));
  }
  if (title) {
    const t = document.createElementNS(NS, "title");
    t.textContent = title;
    svg.append(t);
  }
  for (const [tag, attrs] of ICONS[name] || []) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.append(el);
  }
  return svg;
}
