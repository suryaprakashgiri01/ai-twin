// Chat with every agent. Live turns stream plan, steps, thinking, answer and sources; an old
// conversation replays each answer's stored trace (steps, models, sources and reasoning).

import { isOwner, request, rows, stream } from "../api.js";
import { badge, clear, h, select, toast } from "../dom.js";
import { markdown } from "../markdown.js";
import { refresh, setActive, titleOf } from "../sidebar.js";

const AGENTS = {
  super: { label: "Super", path: "/admin/v1/super", owner: true, settings: "super",
    hint: "Plans the request and runs intake, ask and prep in turn.",
    starters: ["I have an interview with Acme on Friday at 3 pm; add it and give me a 60-second pitch",
      "Summarise the projects I worked on and prep me for a system design round"] },
  ask: { label: "Ask", path: "/admin/v1/chat", owner: true, settings: "owner",
    hint: "Answers from your knowledge base, records and profile, with sources.",
    starters: ["What did I learn at the last meetup?", "How many interviews did I have this month?",
      "Give me a summary of the projects I worked on"] },
  prep: { label: "Prep", path: "/admin/v1/prep", owner: true, settings: "prep",
    hint: "Interview and application answers in your voice, filtered for outward use.",
    starters: ["Tell me about the most challenging project you led", "Why do you want to join this company?",
      "What is your biggest weakness?"] },
  intake: { label: "Intake", path: "/admin/v1/assistant", owner: true, settings: "intake",
    hint: "Turns a message into records or notes; nothing is written before your yes.",
    starters: ["I applied to Acme for an AI Engineer role today", "Note: learned about Raft leader election at the meetup",
      "Show my interviews this week"] },
  public: { label: "Public", path: "/v1/chat", owner: false,
    hint: "The portfolio assistant visitors talk to: public content only.",
    starters: ["What does he build?", "What is his experience with AI systems?", "How can I get in touch?"] },
};
const MODE_AGENT = { ask: "ask", prep: "prep", intake: "intake", super: "super" };

const chat = { agent: "super", model: "", conversation: null, readOnly: false, controller: null };
let els = {};
let models = [];

export async function mount(root) {
  if (!isOwner()) chat.agent = "public";
  const agentPick = select(available().map((a) => [a, AGENTS[a].label]), chat.agent, { "aria-label": "Agent", on: { change: (e) => switchAgent(e.target.value) } });
  const modelPick = select([["", "Auto"]], "", { "aria-label": "Model", on: { change: (e) => { chat.model = e.target.value; } } });
  const input = h("textarea", { rows: 2, placeholder: "Message…", "aria-label": "Message", on: { keydown: onKey } });
  const send = h("button", { class: "btn btn-primary", type: "submit", text: "Send" });
  const stop = h("button", { class: "btn", type: "button", text: "Stop", hidden: true, on: { click: () => chat.controller?.abort() } });
  els = {
    agentPick, modelPick, input, send, stop,
    head: h("div", { class: "chat-head" }),
    log: h("div", { class: "chat-log", "aria-live": "polite" }),
    composer: h("form", { class: "composer", on: { submit: (e) => { e.preventDefault(); sendMessage(input.value); } } },
      input, h("div", { class: "composer-actions" }, stop, send)),
    settings: h("span", { class: "muted small" }),
  };
  clear(root,
    h("div", { class: "chat" },
      h("div", { class: "chat-bar" },
        h("label", { class: "inline" }, h("span", { text: "Agent" }), agentPick),
        isOwner() ? h("label", { class: "inline" }, h("span", { text: "Model" }), modelPick) : null,
        els.settings),
      els.head, els.log, els.composer));
  if (isOwner()) await loadModels();
  fresh();
}

function available() {
  return Object.keys(AGENTS).filter((a) => isOwner() || !AGENTS[a].owner);
}

async function loadModels() {
  const r = await request("GET", "/admin/v1/models");
  models = rows(r).filter((m) => m.enabled && m.roles.includes("generator"));
  clear(els.modelPick, h("option", { value: "", text: "Auto (gateway picks)" }),
    models.map((m) => h("option", { value: m.id, text: `${m.id} · ${m.local ? "local" : m.provider} · ≤${m.maxTier}` })));
  els.modelPick.value = chat.model;
}

async function showSettings() {
  const name = AGENTS[chat.agent].settings;
  if (!isOwner() || !name) { els.settings.textContent = ""; return; }
  const r = await request("GET", "/admin/v1/agents");
  const a = rows(r).find((x) => x.name === name);
  if (!a) { els.settings.textContent = ""; return; }
  const parts = [`model ${a.model}`, a.prefer && `prefer ${a.prefer}`, `confidential ${a.confidentialModel}`, a.need && `need ${a.need}`];
  clear(els.settings, `Agent default: ${parts.filter(Boolean).join(" · ")} (${a.source}) `,
    h("a", { href: "#models", text: "edit", on: { click: () => document.querySelector("[data-tab=models]")?.click() } }));
}

function switchAgent(agent) {
  chat.agent = agent;
  els.agentPick.value = agent;
  fresh();
}

export function fresh() {
  chat.controller?.abort();
  chat.conversation = null;
  chat.readOnly = false;
  setActive(null);
  els.composer.hidden = false;
  els.agentPick.disabled = false;
  const agent = AGENTS[chat.agent];
  clear(els.head, h("div", { class: "chat-title", text: `New ${agent.label} chat` }), h("div", { class: "muted small", text: agent.hint }));
  clear(els.log, h("div", { class: "starters" },
    h("div", { class: "muted small", text: "Try one:" }),
    agent.starters.map((s) => h("button", { class: "chip", type: "button", text: s, on: { click: () => sendMessage(s) } }))));
  showSettings();
  els.input.focus();
}

function onKey(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage(els.input.value);
  }
}

// --- an old conversation -------------------------------------------------------------------

export async function open(c) {
  chat.controller?.abort();
  const path = isOwner() ? `/admin/v1/conversations/${c.id}` : `/v1/conversations/${c.id}`;
  const r = await request("GET", path, { query: { limit: 200 } });
  if (!r.ok) { toast(`Could not open: ${r.error?.code}`); return; }
  const detail = r.data;
  chat.conversation = detail.id;
  chat.readOnly = isOwner() && detail.channel === "public";
  if (detail.channel === "public") chat.agent = "public";
  else chat.agent = MODE_AGENT[detail.mode] || "ask";
  els.agentPick.value = chat.agent;
  els.agentPick.disabled = true;
  els.composer.hidden = chat.readOnly;
  setActive(detail.id);
  clear(els.head,
    h("div", { class: "chat-title", text: titleOf(detail) }),
    h("div", { class: "muted small" }, badge(detail.channel === "public" ? "public" : `owner·${detail.mode}`, detail.channel === "public" ? "warn" : "info"),
      ` ${detail.messageCount} messages`, chat.readOnly ? " · a visitor's chat, read-only" : "",
      detail.summary ? h("details", { class: "summary" }, h("summary", { text: "Summary" }), h("p", { text: detail.summary })) : null));
  clear(els.log);
  for (const turn of detail.turns) {
    if (turn.role === "user") els.log.append(userBubble(turn.content));
    else els.log.append(storedAnswer(turn));
  }
  els.log.scrollTop = els.log.scrollHeight;
  showSettings();
}

function storedAnswer(turn) {
  const view = answerView();
  const trace = turn.trace;
  if (trace?.steps?.length) {
    for (const [i, step] of trace.steps.entries()) {
      const item = view.step(i, step.agent);
      item.set(step);
      if (step.thinking) item.reasoning(step.thinking);
    }
  }
  if (trace?.thinking) view.reasoning(trace.thinking);
  view.answer(turn.content);
  const sources = trace?.sources?.length ? trace.sources : (turn.citations || []).map((id, n) => ({ index: n + 1, documentId: id, title: id }));
  view.sources(sources);
  view.footer([
    turn.model && `model ${turn.model}`, turn.maxTier && `tier ${turn.maxTier}`,
    turn.route && `route ${turn.route}`, turn.latencyMs && `${Math.round(turn.latencyMs)} ms`,
    turn.incomplete && "incomplete", turn.withheld && `${turn.withheld} withheld`, turn.cacheHit && "cached",
  ]);
  return view.el;
}

// --- a live turn -----------------------------------------------------------------------------

async function sendMessage(text) {
  const message = String(text || "").trim();
  if (!message || chat.controller || chat.readOnly) return;
  els.input.value = "";
  if (!chat.conversation) clear(els.log);
  els.log.append(userBubble(message));
  const view = answerView({ live: true });
  els.log.append(view.el);
  scroll();
  const agent = AGENTS[chat.agent];
  const body = { message };
  if (chat.conversation) body.conversationId = chat.conversation;
  if (chat.model && agent.owner && chat.agent !== "intake") body.model = chat.model;
  await run(agent, body, view);
}

async function run(agent, body, view) {
  chat.controller = new AbortController();
  els.send.disabled = true;
  els.stop.hidden = false;
  els.agentPick.disabled = true;
  let answer = "";
  const started = performance.now();
  const outcome = await stream(agent.path, body, (event, data) => {
    if (data.conversationId && !chat.conversation) {
      chat.conversation = data.conversationId;
      setActive(chat.conversation);
    }
    switch (event) {
      case "meta": view.meta(data); break;
      case "step": view.step(data.index, data.agent).set(data); break;
      case "thinking":
        if (data.index !== undefined) view.step(data.index, data.agent).reasoning(data.text, true);
        else view.reasoning(data.text, true);
        break;
      case "token": answer += data.text || ""; view.answer(answer); break;
      case "sources": view.sources(data.sources || []); break;
      case "draft": view.draft(data, (reply) => replyTo(agent, reply)); break;
      case "done": view.done(data, Math.round(performance.now() - started)); break;
      case "error": view.error(data); break;
      default: break;
    }
    scroll();
  }, chat.controller.signal);
  if (outcome.aborted) view.error({ code: "STOPPED", message: "Stopped by you" });
  view.finish();
  chat.controller = null;
  els.send.disabled = false;
  els.stop.hidden = true;
  refresh();
}

// The owner's yes or no to an intake draft is the next message of the same conversation.
function replyTo(agent, reply) {
  if (chat.controller) return;
  const next = answerView({ live: true });
  els.log.append(userBubble(reply), next.el);
  run(agent, { message: reply, conversationId: chat.conversation }, next);
}

function scroll() {
  const nearBottom = els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 160;
  if (nearBottom) els.log.scrollTop = els.log.scrollHeight;
}

function userBubble(text) {
  return h("div", { class: "msg msg-user" }, h("div", { class: "bubble", text }));
}

// --- one assistant answer: status, steps, reasoning, answer, sources, footer ------------------

function answerView({ live = false } = {}) {
  const status = h("div", { class: "status-line", hidden: !live }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { class: "status-text", text: "Thinking…" }));
  const steps = h("ol", { class: "steps", hidden: true });
  const thinking = h("div", { class: "reasoning-slot" });
  const body = h("div", { class: "answer" });
  const extras = h("div", { class: "extras" });
  const chips = h("div", { class: "chips sources", hidden: true });
  const footer = h("div", { class: "msg-foot muted small" });
  const el = h("div", { class: "msg msg-assistant" }, status, steps, thinking, body, extras, chips, footer);
  const stepItems = new Map();
  let reasoningBox = null;

  const setStatus = (text) => { status.querySelector(".status-text").textContent = text; };
  const cite = (n) => {
    const chip = chips.querySelector(`[data-n="${n}"]`);
    if (chip) { chip.classList.add("flash"); chip.scrollIntoView({ block: "nearest" }); setTimeout(() => chip.classList.remove("flash"), 1200); }
  };

  return {
    el,
    meta(data) {
      if (data.plan?.length) {
        setStatus(`Plan: ${data.plan.map((s) => s.agent).join(" → ")}${data.planSource ? ` (${data.planSource})` : ""}`);
        data.plan.forEach((s, i) => this.step(i, s.agent).set({ status: "planned", instruction: s.instruction }));
      } else if (data.route) setStatus(`Route: ${data.route}${data.maxTier ? ` · up to ${data.maxTier}` : ""}`);
      else if (data.kind) setStatus(`Prep: ${data.kind}${data.queries?.length ? ` · ${data.queries.length} searches` : ""}`);
      else if (data.intent) setStatus(`Intent: ${data.intent}`);
      else setStatus("Working…");
    },
    step(index, agent) {
      if (!stepItems.has(index)) {
        const head = h("div", { class: "step-head" });
        const slot = h("div", { class: "reasoning-slot" });
        const li = h("li", { class: "step" }, head, slot);
        let box = null;
        stepItems.set(index, {
          set(data) {
            const st = data.status || "done";
            clear(head, h("span", { class: `step-dot st-${st}` }), h("strong", { text: data.agent || agent }),
              badge(st, st === "done" ? "ok" : st === "error" ? "err" : st === "pending" ? "warn" : ""),
              data.model ? h("span", { class: "muted small", text: ` ${data.model}` }) : null,
              data.tier ? h("span", { class: "muted small", text: ` · ${data.tier}` }) : null,
              data.action ? h("span", { class: "muted small", text: ` · ${data.action}` }) : null,
              data.instruction ? h("div", { class: "muted small step-instruction", text: data.instruction }) : null);
            if (st === "running") setStatus(`Running ${data.agent || agent}…`);
          },
          reasoning(text, appendLive = false) {
            if (!box) { box = reasoning({ live: appendLive }); slot.append(box.el); }
            box.add(text);
          },
          finish() { box?.finish(); },
        });
        stepItems.get(index).set({ agent, status: "planned" });
        steps.append(li);
        steps.hidden = false;
      }
      return stepItems.get(index);
    },
    reasoning(text, appendLive = false) {
      if (!reasoningBox) { reasoningBox = reasoning({ live: appendLive }); thinking.append(reasoningBox.el); }
      reasoningBox.add(text);
    },
    answer(text) {
      clear(body, markdown(text, cite));
      if (text) setStatus("Answering…");
    },
    sources(list) {
      clear(chips, list.map((s) => h("span", { class: "chip source", dataset: { n: String(s.index) }, title: [s.category, s.tier, s.documentId].filter(Boolean).join(" · "), text: `[${s.index}] ${s.title || s.documentId}` })));
      chips.hidden = !list.length;
    },
    draft(data, reply) {
      const yes = h("button", { class: "btn btn-primary btn-sm", type: "button", text: "Yes, save it" });
      const no = h("button", { class: "btn btn-sm", type: "button", text: "No" });
      const answerWith = (word) => { yes.disabled = true; no.disabled = true; reply(word); };
      yes.addEventListener("click", () => answerWith("yes"));
      no.addEventListener("click", () => answerWith("no"));
      extras.append(h("div", { class: "draft card" },
        h("div", { class: "card-title" }, badge(data.kind || "draft", "warn"), h("strong", { text: data.action || "" })),
        data.summary ? markdown(data.summary) : null,
        data.prompt ? h("p", { class: "muted", text: data.prompt }) : null,
        h("div", { class: "row" }, yes, no)));
    },
    done(data, ms) {
      if (data.checks?.length) extras.append(h("div", { class: "note" }, h("strong", { text: "Check before sending" }), h("ul", {}, data.checks.map((c) => h("li", { text: c })))));
      if (data.removed?.length) extras.append(h("div", { class: "note" }, h("strong", { text: "Internal details removed" }), h("ul", {}, data.removed.map((c) => h("li", { text: c })))));
      this.footer([data.model && `model ${data.model}`, data.tier && `tier ${data.tier}`,
        data.route && `route ${data.route}`, `${data.latencyMs ? Math.round(data.latencyMs) : ms} ms`,
        data.withheld && `${data.withheld} withheld`, data.incomplete && "incomplete", data.pending && "waiting for your reply",
        data.cacheHit && "cached"]);
    },
    error(data) {
      extras.append(h("div", { class: "error-line", text: `${data.code}: ${data.message}` }));
    },
    footer(parts) { footer.textContent = parts.filter(Boolean).join(" · "); },
    finish() {
      status.hidden = true;
      reasoningBox?.finish();
      for (const item of stepItems.values()) item.finish();
    },
  };
}

// A reasoning item: the first lines, and the whole of it on click. Live, it follows the stream.
function reasoning({ live = false } = {}) {
  let text = "";
  let expanded = false;
  const label = h("span", { class: "reasoning-label", text: live ? "Thinking…" : "Reasoning" });
  const size = h("span", { class: "muted small" });
  const preview = h("div", { class: "reasoning-text" });
  const toggle = h("button", { class: "reasoning-head", type: "button", "aria-expanded": "false", on: { click: () => { expanded = !expanded; paint(); } } },
    h("span", { class: "chev", "aria-hidden": "true", text: "▸" }), label, size);
  const el = h("div", { class: "reasoning" }, toggle, preview);

  function paint() {
    toggle.setAttribute("aria-expanded", String(expanded));
    el.classList.toggle("open", expanded);
    const lines = text.trim().split("\n").filter((l) => l.trim());
    const shown = expanded ? text.trim() : (live ? lines.slice(-3) : lines.slice(0, 3)).join("\n");
    preview.textContent = shown;
    const more = !expanded && lines.length > 3;
    preview.classList.toggle("clipped", more);
    size.textContent = ` · ${text.length.toLocaleString()} chars${more ? " · click to expand" : ""}`;
  }
  return {
    el,
    add(chunk) { text += chunk; paint(); },
    finish() { live = false; label.textContent = "Reasoning"; paint(); },
  };
}
