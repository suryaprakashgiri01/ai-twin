// Every call the console makes goes through here. The key is sent as X-API-Key to this
// origin only; it is kept in sessionStorage, or localStorage when "remember" is ticked.

const KEY = "twin.apiKey";

function read(storage) {
  try { return storage.getItem(KEY) || ""; } catch { return ""; }
}

export const state = {
  key: read(sessionStorage) || read(localStorage),
  remembered: Boolean(read(localStorage)),
  me: null, // { name, scopes, role } from GET /v1/me
};

export function setKey(key, remember) {
  state.key = key;
  state.remembered = remember;
  try {
    sessionStorage.removeItem(KEY);
    localStorage.removeItem(KEY);
    if (key) (remember ? localStorage : sessionStorage).setItem(KEY, key);
  } catch { /* storage blocked: the key lives for this page only */ }
}

export const isOwner = () => state.me?.role === "owner";

function query(url, params) {
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== "" && v !== undefined && v !== null) url.searchParams.set(k, v);
  }
  return url;
}

/**
 * request("GET", "/admin/v1/records", { body, query, form })
 * Resolves with { status, ok, data (the envelope's result), message, error } and never throws
 * (status 0: the network call itself failed). `form` sends FormData (file uploads).
 */
export async function request(method, path, opts = {}) {
  const { body, form, key = state.key } = opts;
  const url = query(new URL(path, window.location.origin), opts.query);
  const headers = {};
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let status = 0;
  let parsed = null;
  try {
    const res = await fetch(url, {
      method,
      headers,
      body: form ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
    status = res.status;
    const text = await res.text();
    try { parsed = text ? JSON.parse(text) : null; } catch { parsed = { message: text }; }
  } catch (err) {
    parsed = { message: String(err), error: { code: "NETWORK_ERROR" } };
  }
  const ok = status >= 200 && status < 300;
  return {
    status,
    ok,
    data: ok ? parsed?.result ?? parsed : null,
    message: parsed?.message || "",
    error: ok ? null : parsed?.error || { code: `HTTP_${status}` },
  };
}

/** The rows of a list page (`listOfItems`), or a plain list result. */
export function rows(r) {
  if (!r.ok) return [];
  return Array.isArray(r.data) ? r.data : r.data?.listOfItems || [];
}

/**
 * stream("/admin/v1/chat", body, (event, data) => …, signal)
 * POSTs and reads Server-Sent Events with fetch (EventSource cannot send the key header).
 * Resolves with { ok, status, error } once the stream ends; a JSON error before streaming
 * arrives as one "error" event with the envelope's code and message.
 */
export async function stream(path, body, onEvent, signal) {
  let res;
  try {
    res = await fetch(new URL(path, window.location.origin), {
      method: "POST",
      headers: { "X-API-Key": state.key, "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") return { ok: false, status: 0, aborted: true };
    onEvent("error", { code: "NETWORK_ERROR", message: String(err) });
    return { ok: false, status: 0 };
  }
  const type = res.headers.get("content-type") || "";
  if (!res.ok || !type.startsWith("text/event-stream")) {
    let parsed = null;
    try { parsed = await res.json(); } catch { /* not JSON */ }
    const error = { code: parsed?.error?.code || `HTTP_${res.status}`, message: parsed?.message || res.statusText };
    onEvent("error", error);
    return { ok: false, status: res.status, error };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        dispatch(frame, onEvent);
      }
    }
  } catch (err) {
    if (err.name === "AbortError") return { ok: false, status: res.status, aborted: true };
    onEvent("error", { code: "STREAM_INTERRUPTED", message: String(err) });
    return { ok: false, status: res.status };
  }
  if (buffer.trim()) dispatch(buffer, onEvent);
  return { ok: true, status: res.status };
}

function dispatch(frame, onEvent) {
  let event = "message";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return;
  let parsed;
  try { parsed = JSON.parse(data.join("\n")); } catch { parsed = { text: data.join("\n") }; }
  onEvent(event, parsed);
}
