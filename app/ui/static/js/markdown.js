// Markdown to DOM nodes, never to an HTML string: headings, lists, quotes, fenced and inline
// code, bold, italic, links (http/https/mailto only, rel=noopener) and citations [n].

import { h } from "./dom.js";

const SAFE_LINK = /^(https?:|mailto:)/i;

/** Render `text` into a fragment. `onCite(n)` is called when a citation [n] is clicked. */
export function markdown(text, onCite) {
  const out = document.createDocumentFragment();
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```\s*([\w+-]*)\s*$/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i += 1;
      out.append(h("pre", { class: "code" }, h("code", { text: body.join("\n") })));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      out.append(h(`h${Math.min(heading[1].length + 2, 6)}`, {}, inline(heading[2], onCite)));
      i += 1;
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) {
        list.append(h("li", {}, inline(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, ""), onCite)));
        i += 1;
      }
      out.append(list);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      out.append(h("blockquote", {}, inline(quote.join(" "), onCite)));
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) para.push(lines[i++]);
    const p = h("p");
    para.forEach((part, n) => {
      if (n) p.append(h("br"));
      p.append(inline(part, onCite));
    });
    out.append(p);
  }
  return out;
}

function isBlockStart(line) {
  return /^```/.test(line) || /^#{1,4}\s/.test(line) || /^\s*([-*]|\d+[.)])\s+/.test(line) || /^>\s?/.test(line);
}

// Inline tokens, earliest match first: code, link, bold, italic, citation.
const INLINE = /(`[^`]+`)|\[([^\]]+)\]\(([^)\s]+)\)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|\[(\d{1,3})\]/;

function inline(text, onCite) {
  const frag = document.createDocumentFragment();
  let rest = text;
  while (rest) {
    const m = rest.match(INLINE);
    if (!m) { frag.append(rest); break; }
    if (m.index) frag.append(rest.slice(0, m.index));
    if (m[1]) frag.append(h("code", { text: m[1].slice(1, -1) }));
    else if (m[2]) {
      frag.append(SAFE_LINK.test(m[3])
        ? h("a", { href: m[3], target: "_blank", rel: "noopener noreferrer", text: m[2] })
        : m[0]);
    } else if (m[4]) frag.append(h("strong", {}, inline(m[4].slice(2, -2), onCite)));
    else if (m[5]) frag.append(h("em", {}, inline(m[5].slice(1, -1), onCite)));
    else if (m[6]) {
      const n = Number(m[6]);
      frag.append(onCite
        ? h("button", { class: "cite", type: "button", text: `[${n}]`, on: { click: () => onCite(n) } })
        : h("sup", { class: "cite", text: `[${n}]` }));
    }
    rest = rest.slice(m.index + m[0].length);
  }
  return frag;
}
