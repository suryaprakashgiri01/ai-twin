"""Chat with the vault from the terminal: `python -m scripts.chat [list|new|twin|continue|rename]`.

  list [--limit N]                         your conversations: name, mode, id, updated, turns, ...
  new                                      ask the vault questions (the default)
  twin                                     add knowledge in plain language (intake assistant)
  continue <name|id>                       recap the last turns, then keep going in that mode
                                           (an intake conversation resumes at its pending draft)
  rename <name|id> <new-name> [--description TEXT]

The intake assistant (`twin`) asks for anything missing, shows a draft and writes nothing until
you answer yes; deletes also ask you to retype the title or company.

It talks to the running API (`make run`) with the admin key in AI_TWIN_ADMIN_KEY (.env or the
environment); the key is never printed. Context is the server's job: it reads the conversation
from Redis/Valkey while the TTL lasts and from MongoDB after that. Type /exit (or Ctrl+C) to leave.
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scripts._common import fail, info, ok, paint, read_env, warn
from scripts._thinking import ASK_PHRASES, INTAKE_PHRASES, TITLE_PHRASES, Thinking

KEY_NAME = "AI_TWIN_ADMIN_KEY"
CONVERSATION_ID = re.compile(r"^[0-9a-f]{32}$")
EXIT_WORDS = frozenset({"/exit", "/quit", "exit", "quit"})
RECAP_TURNS = 4
RECAP_CHARS = 280
LIST_LIMIT_MAX = 200
# Client timeout while the server chooses how long to wait (local models get longer).
TITLE_REFRESH_TIMEOUT = 45.0
# Names are at most 48 characters; generated ones at most 40 plus a short suffix.
NAME_WIDTH = 44
MODE_WIDTH = 6


class CliError(Exception):
    """A problem worth one clear line, not a traceback."""


@dataclass(frozen=True)
class Config:
    base_url: str
    key: str


def load_config(base_url: str | None, env: dict[str, str] | None = None) -> Config:
    values = {**(env if env is not None else read_env()), **os.environ}
    key = values.get(KEY_NAME, "").strip()
    if not key:
        raise CliError(
            f"{KEY_NAME} is not set. Create an admin key "
            '(make keys ARGS="create owner --scope admin"; Windows: '
            "./scripts/dev.ps1 keys create owner --scope admin) and add "
            f"{KEY_NAME}=<key> to .env"
        )
    host = values.get("HOST", "127.0.0.1") or "127.0.0.1"
    port = values.get("PORT", "8000") or "8000"
    return Config(base_url=(base_url or f"http://{host}:{port}").rstrip("/"), key=key)


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Server-Sent Events as (event, data) pairs; an event ends at a blank line."""
    event, data = "message", ""
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield event, json.loads(data)
            event, data = "message", ""
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()
    if data:
        yield event, json.loads(data)


class Api:
    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None) -> None:
        self._base = config.base_url
        self._client = httpx.Client(
            base_url=f"{config.base_url}/admin/v1",
            headers={"X-API-Key": config.key},
            timeout=httpx.Timeout(10.0, read=300.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == 401:
            raise CliError(f"The API rejected {KEY_NAME} (401). Check the key in .env.")
        if response.status_code == 403:
            raise CliError(f"{KEY_NAME} lacks the admin scope (403).")
        if response.status_code >= 400:
            try:
                message = response.json()["message"]
            except (ValueError, KeyError, TypeError):
                message = response.text[:200]
            raise CliError(f"{response.status_code}: {message}")
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._check(self._client.request(method, path, **kwargs))
        except httpx.ConnectError as exc:
            raise CliError(
                f"The API is not reachable at {self._base}. Start it: make run "
                "(Windows: ./scripts/dev.ps1 run)"
            ) from exc

    def list(self, limit: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._request(
            "GET", "/conversations", params={"limit": limit}
        ).json()
        return result

    def get(self, conversation_id: str, limit: int = 200) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = self._request(
                "GET", f"/conversations/{conversation_id}", params={"limit": limit}
            ).json()
        except CliError as exc:
            if str(exc).startswith("404"):
                return None
            raise
        return result

    def by_name(self, name: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = self._request("GET", f"/conversations/by-name/{name}").json()
        except CliError as exc:
            if str(exc).startswith(("404", "422")):
                return None
            raise
        return result

    def rename(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> dict[str, Any]:
        body = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
        result: dict[str, Any] = self._request(
            "PATCH", f"/conversations/{conversation_id}", json=body
        ).json()
        return result

    def whoami(self) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", "/whoami").json()
        return result

    def version(self) -> str:
        """The API's version from /healthz (outside /admin/v1); "?" when it doesn't say."""
        try:
            response = self._client.get(f"{self._base}/healthz")
        except httpx.ConnectError as exc:
            raise CliError(
                f"The API is not reachable at {self._base}. Start it: make run "
                "(Windows: ./scripts/dev.ps1 run)"
            ) from exc
        return str(response.json().get("version", "?")) if response.is_success else "?"

    @property
    def base_url(self) -> str:
        return self._base

    def refresh_title(self, conversation_id: str) -> dict[str, Any]:
        """Let the server pick the wait (longer for intake / local models)."""
        result: dict[str, Any] = self._request(
            "POST",
            f"/conversations/{conversation_id}/title",
            timeout=TITLE_REFRESH_TIMEOUT,
        ).json()
        return result

    def memories(self, status: str | None = None) -> builtins.list[dict[str, Any]]:
        params = {"status": status} if status else {}
        result: builtins.list[dict[str, Any]] = self._request(
            "GET", "/memories", params=params
        ).json()
        return result

    def remember(
        self, text: str, kind: str = "preference", tier: str = "private"
    ) -> dict[str, Any]:
        body = {"text": text, "kind": kind, "tier": tier}
        result: dict[str, Any] = self._request("POST", "/memories", json=body).json()
        return result

    def update_memory(self, memory_id: str, **changes: Any) -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "PATCH", f"/memories/{memory_id}", json=changes
        ).json()
        return result

    def delete_memory(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{memory_id}")

    def assistant_state(self, conversation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", f"/assistant/{conversation_id}").json()
        return result

    def chat(
        self, message: str, conversation_id: str | None, path: str = "/chat"
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        body: dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        try:
            with self._client.stream("POST", path, json=body) as response:
                if response.status_code >= 400:
                    response.read()
                    self._check(response)
                yield from parse_sse(response.iter_lines())
        except httpx.ConnectError as exc:
            raise CliError(f"The API is not reachable at {self._base}.") from exc


def resolve(api: Api, ref: str) -> dict[str, Any]:
    """A conversation by id, exact name, or unique name prefix."""
    if CONVERSATION_ID.match(ref):
        found = api.get(ref, limit=1)
        if found is None:
            raise CliError(f"No conversation with id {ref}")
        return found
    exact = api.by_name(ref)
    if exact is not None:
        return exact
    matches = [c for c in api.list(LIST_LIMIT_MAX) if (c.get("name") or "").startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CliError(f"No conversation named '{ref}'. See: make chat-list")
    names = ", ".join(str(c.get("name")) for c in matches[:10])
    raise CliError(f"'{ref}' matches {len(matches)} conversations: {names}")


def _short(text: str, width: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= width else line[: width - 1] + "…"


def render_list(conversations: Sequence[dict[str, Any]]) -> str:
    if not conversations:
        return "No conversations yet. Start one: make chat (Windows: ./scripts/dev.ps1 chat)"
    rows = [
        f"{'NAME':<{NAME_WIDTH}} {'MODE':<{MODE_WIDTH}} {'ID':<32} {'UPDATED':<16} {'TURNS':>5}  "
        "DESCRIPTION"
    ]
    for c in conversations:
        updated = str(c.get("updated_at", ""))[:16].replace("T", " ")
        name = _short(str(c.get("name") or "-"), NAME_WIDTH)
        mode = str(c.get("mode") or "ask")
        rows.append(
            f"{name:<{NAME_WIDTH}} {mode:<{MODE_WIDTH}} {c['id']:<32} {updated:<16} "
            f"{c.get('message_count', 0):>5}  {_short(str(c.get('description') or ''), 60)}"
        )
    return "\n".join(rows)


ASK_INTRO = (
    "Ask anything about your vault: notes, projects, documents, records (applications, "
    "assessments, interviews, offers, rejections, withdrawals, HR calls, invites) and your "
    "profile.",
    "Answers cite their sources. Confidential sources are read by the local model only; "
    "private ones may use Groq/Cerebras.",
    'Try: "How many interviews did I have this month?" · "What did I learn building <project>?"',
)
INTAKE_INTRO = (
    "Tell me what to add or change in plain language. I ask for anything missing, show a "
    "draft, and save nothing until you say yes. Runs on your local model only.",
    "I can: add notes · ingest a URL, an inbox file (INTAKE_INBOX_DIR, default vault/inbox) "
    "or a GitHub repo · add, update or delete records (invite from Naukri/LinkedIn, "
    "application, HR call with contact, assessment, interview, offer, rejection, withdrawal) · "
    "update your profile · show the review queue and leads · "
    "approve, publish, unpublish, relabel or delete documents · answer questions.",
    'Try: "I had a technical round 2 with Acme yesterday for an AI Engineer role"',
)
COMMANDS = (
    "Commands: /remember <text> (followed from your next message) · /memory (what I remember) · "
    "/exit (or Ctrl+C) leaves; the conversation is kept for make chat-continue."
)
REMEMBER = "/remember"
MEMORY = "/memory"
TWIN = "twin: "
SUMMARY_CHARS = 400
MEMORY_HINT = "make memory (Windows: ./scripts/dev.ps1 memory)"


def _when(value: object) -> str:
    return str(value or "")[:16].replace("T", " ") or "-"


def print_banner(api: Api, mode: str, conversation: dict[str, Any] | None = None) -> None:
    """Printed before the first prompt: where you are, what this agent does, how to leave.

    It also proves the API answers and the key works before you type anything."""
    who = api.whoami()
    title = "intake assistant" if mode == "intake" else "owner chat"
    print(paint(f"━━ ai-twin · {title} ━━", "bold"))
    print(paint(f'API {api.version()} at {api.base_url} · key "{who.get("name", "?")}"', "dim"))
    if conversation is None:
        for line in INTAKE_INTRO if mode == "intake" else ASK_INTRO:
            print(line)
    else:
        print(
            f"Continuing {paint(str(conversation.get('name') or '-'), 'bold')} "
            f"({mode}) · id {conversation['id']}"
        )
        turns = conversation.get("message_count", 0)
        print(
            f"Started {_when(conversation.get('created_at'))} · last active "
            f"{_when(conversation.get('updated_at'))} · {turns} turns"
        )
        if conversation.get("description"):
            print(f"About: {_short(str(conversation['description']), 100)}")
        if conversation.get("summary"):
            covered = conversation.get("summarized_turns", 0)
            summary = _short(str(conversation["summary"]), SUMMARY_CHARS)
            print(f"Summary of the first {covered} turns: {summary}")
        print(
            paint(
                "Earlier turns are reloaded by the server: from the Valkey cache while it lasts "
                "(14 days), otherwise from MongoDB.",
                "dim",
            )
        )
    print(paint(COMMANDS, "dim"))


def print_recap(conversation: dict[str, Any]) -> None:
    turns = conversation.get("turns", [])[-RECAP_TURNS:]
    if turns:
        info(f"Last {len(turns)} turns:")
    for turn in turns:
        who = paint("you", "blue") if turn.get("role") == "user" else paint("twin", "green")
        print(f"  {who}: {_short(str(turn.get('content', '')), RECAP_CHARS)}")


def _done_line(data: dict[str, Any]) -> str:
    parts = [f"tier {data.get('tier') or '-'}", f"model {data.get('model') or '-'}"]
    if data.get("latency_ms") is not None:
        parts.append(f"{float(data['latency_ms']) / 1000:.1f}s")
    if data.get("withheld"):
        parts.append(f"{data['withheld']} withheld")
    return paint("  " + " · ".join(parts), "dim")


def _thinking(phrases: Sequence[str]) -> Thinking:
    return Thinking(phrases, paint(TWIN, "green"), TWIN)


def ask(api: Api, message: str, conversation_id: str | None) -> str | None:
    """Stream one answer to the terminal; returns the conversation id."""
    with _thinking(ASK_PHRASES) as thinking:
        for event, data in api.chat(message, conversation_id):
            conversation_id = data.get("conversation_id", conversation_id)
            if event in ("token", "sources", "done", "error"):
                thinking.stop()
            _show_answer_event(event, data)
    return conversation_id


def _show_answer_event(event: str, data: dict[str, Any]) -> None:
    if event == "token":
        print(data.get("text", ""), end="", flush=True)
    elif event == "sources" and data.get("sources"):
        titles = "; ".join(f"[{s['index']}] {s['title']}" for s in data["sources"])
        print("\n" + paint(f"  sources: {titles}", "dim"), end="")
    elif event == "done":
        print("\n" + _done_line(data))
    elif event == "error":
        print()
        warn(f"{data.get('code')}: {data.get('message')}")


def print_draft(draft: dict[str, Any]) -> None:
    print(paint("  " + json.dumps(draft.get("payload", {}), ensure_ascii=False), "dim"))
    print(paint(str(draft.get("prompt", "")), "yellow"))


def tell(api: Api, message: str, conversation_id: str | None) -> str | None:
    """One intake turn: the reply, a pending draft if any; returns the conversation id."""
    with _thinking(INTAKE_PHRASES) as thinking:
        for event, data in api.chat(message, conversation_id, path="/assistant"):
            conversation_id = data.get("conversation_id", conversation_id)
            if event in ("token", "draft", "done", "error"):
                thinking.stop()
            _show_intake_event(event, data)
    return conversation_id


def _show_intake_event(event: str, data: dict[str, Any]) -> None:
    if event == "token":
        print(data.get("text", ""), end="", flush=True)
    elif event == "draft":
        print()
        print_draft(data)
    elif event == "done":
        if not data.get("pending"):
            print()
        parts = [str(data.get("status")), str(data.get("action") or "-"), "local model"]
        print(paint("  " + " · ".join(parts), "dim"))
    elif event == "error":
        print()
        warn(f"{data.get('code')}: {data.get('message')}")


def print_pending(api: Api, conversation_id: str) -> None:
    pending = api.assistant_state(conversation_id).get("pending")
    if pending:
        info("Waiting for your answer:")
        print_draft(pending)


def refresh_on_exit(api: Api, conversation_id: str) -> str | None:
    """Ask the server to re-title the conversation; returns its (possibly new) name."""
    print(paint("Updating the conversation's title…", "dim"))
    try:
        with Thinking(TITLE_PHRASES):
            result = api.refresh_title(conversation_id)
    except (CliError, httpx.HTTPError, KeyboardInterrupt) as exc:
        warn(f"Title not refreshed ({type(exc).__name__}); the conversation is kept as it was")
        return None
    status = result.get("status")
    new = int(result.get("new_memories") or 0)
    if new:
        noun = "memory" if new == 1 else "memories"
        info(f"{new} new {noun} to review: {MEMORY_HINT}")
    if status == "updated" and result.get("renamed"):
        ok(f"Renamed to {paint(str(result.get('name')), 'bold')}")
    if status == "updated" and result.get("description"):
        info(f"About: {result['description']}")
    elif status == "scheduled":
        info("The local model is still titling it; the new title shows in make chat-list")
    name = result.get("name")
    return str(name) if name else None


def render_memories(items: Sequence[dict[str, Any]]) -> str:
    if not items:
        return "Nothing remembered yet. Add one: /remember <text>"
    rows = []
    for m in items:
        flag = "" if m.get("status") == "active" else f" [{m.get('status')}]"
        tier = " (confidential)" if m.get("tier") == "confidential" else ""
        rows.append(f"  {m['id']}  {m.get('kind', '?'):<11} {m.get('text', '')}{flag}{tier}")
    return "\n".join(rows)


def memory_command(api: Api, message: str) -> bool:
    """Handle /remember and /memory; returns False for any other message."""
    command, _, rest = message.partition(" ")
    if command == REMEMBER:
        text = rest.strip()
        if not text:
            warn("Usage: /remember <what to remember>")
            return True
        try:
            item = api.remember(text)
        except CliError as exc:
            warn(str(exc))
            return True
        ok(f"Remembered ({item.get('kind')}, {item.get('id')}); followed from your next message")
        return True
    if command == MEMORY and not rest.strip():
        print(render_memories(api.memories()))
        return True
    return False


def chat_loop(api: Api, conversation_id: str | None, mode: str = "ask") -> int:
    turn = tell if mode == "intake" else ask
    named = conversation_id is not None
    spoke = False
    while True:
        try:
            message = input(paint("you: ", "blue")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message.lower() in EXIT_WORDS:
            break
        if memory_command(api, message):
            continue
        try:
            conversation_id = turn(api, message, conversation_id)
            spoke = True
        except KeyboardInterrupt:
            print()
            warn("Answer interrupted")
            continue
        if not named and conversation_id:
            found = api.get(conversation_id, limit=1)
            if found and found.get("name"):
                info(
                    f"Saved as {paint(found['name'], 'bold')} (continue later: make chat-continue "
                    f'ARGS="{found["name"]}")'
                )
            named = True
    if conversation_id:
        name = refresh_on_exit(api, conversation_id) if spoke else None
        ref = name or conversation_id
        info(f'Conversation kept · continue later: make chat-continue ARGS="{ref}"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", help="API address (default: http://HOST:PORT from .env)")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("list", help="list conversations")
    listing.add_argument("--limit", type=int, default=20)
    sub.add_parser("new", help="start a conversation")
    sub.add_parser("twin", help="add knowledge in plain language (intake assistant)")
    resume = sub.add_parser("continue", help="continue a conversation")
    resume.add_argument("ref", help="name, unique name prefix, or id")
    rename = sub.add_parser("rename", help="rename a conversation")
    rename.add_argument("ref")
    rename.add_argument("name")
    rename.add_argument("--description")
    return parser


def execute(args: argparse.Namespace, api: Api) -> int:
    command = args.command or "new"
    if command == "list":
        print(render_list(api.list(max(1, min(args.limit, LIST_LIMIT_MAX)))))
        return 0
    if command == "continue":
        found = resolve(api, args.ref)
        detail = api.get(found["id"]) or found
        mode = str(detail.get("mode") or "ask")
        print_banner(api, mode, detail)
        print_recap(detail)
        if mode == "intake":
            print_pending(api, found["id"])
        return chat_loop(api, found["id"], mode)
    if command == "rename":
        found = resolve(api, args.ref)
        updated = api.rename(found["id"], args.name, args.description)
        ok(f"Renamed to {updated.get('name')}")
        return 0
    if command == "twin":
        print_banner(api, "intake")
        return chat_loop(api, None, "intake")
    print_banner(api, "ask")
    return chat_loop(api, None)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        api = Api(load_config(args.base_url))
    except CliError as exc:
        fail(str(exc))
        return 1
    try:
        return execute(args, api)
    except CliError as exc:
        fail(str(exc))
        return 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
