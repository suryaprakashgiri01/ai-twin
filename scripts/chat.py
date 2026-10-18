"""Chat with the vault from the terminal: `python -m scripts.chat [list|new|continue|rename]`.

  list [--limit N]                         your conversations: name, id, updated, turns, description
  new                                      start a conversation (the default)
  continue <name|id>                       recap the last turns, then keep chatting
  rename <name|id> <new-name> [--description TEXT]

It talks to the running API (`make run`) with the admin key in AI_TWIN_ADMIN_KEY (.env or the
environment); the key is never printed. Context is the server's job: it reads the conversation
from Redis/Valkey while the TTL lasts and from MongoDB after that. Type /exit (or Ctrl+C) to leave.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scripts._common import fail, info, ok, paint, read_env, warn

KEY_NAME = "AI_TWIN_ADMIN_KEY"
CONVERSATION_ID = re.compile(r"^[0-9a-f]{32}$")
EXIT_WORDS = frozenset({"/exit", "/quit", "exit", "quit"})
RECAP_TURNS = 4
RECAP_CHARS = 280
LIST_LIMIT_MAX = 200
# Names are at most 48 characters; generated ones at most 40 plus a short suffix.
NAME_WIDTH = 44


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
                message = response.json()["error"]["message"]
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

    def chat(
        self, message: str, conversation_id: str | None
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        body: dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        try:
            with self._client.stream("POST", "/chat", json=body) as response:
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
    rows = [f"{'NAME':<{NAME_WIDTH}} {'ID':<32} {'UPDATED':<16} {'TURNS':>5}  DESCRIPTION"]
    for c in conversations:
        updated = str(c.get("updated_at", ""))[:16].replace("T", " ")
        name = _short(str(c.get("name") or "-"), NAME_WIDTH)
        rows.append(
            f"{name:<{NAME_WIDTH}} {c['id']:<32} {updated:<16} {c.get('message_count', 0):>5}  "
            f"{_short(str(c.get('description') or ''), 60)}"
        )
    return "\n".join(rows)


def print_recap(conversation: dict[str, Any]) -> None:
    turns = conversation.get("turns", [])[-RECAP_TURNS:]
    title = conversation.get("name") or conversation["id"]
    info(f"Continuing {paint(str(title), 'bold')} ({conversation.get('message_count', 0)} turns)")
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


def ask(api: Api, message: str, conversation_id: str | None) -> str | None:
    """Stream one answer to the terminal; returns the conversation id."""
    print(paint("twin: ", "green"), end="", flush=True)
    for event, data in api.chat(message, conversation_id):
        conversation_id = data.get("conversation_id", conversation_id)
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
    return conversation_id


def chat_loop(api: Api, conversation_id: str | None) -> int:
    print(paint("Type your message; /exit to leave.", "dim"))
    named = conversation_id is not None
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
        try:
            conversation_id = ask(api, message, conversation_id)
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
        info(f"Conversation {conversation_id} kept")
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
        print_recap(detail)
        return chat_loop(api, found["id"])
    if command == "rename":
        found = resolve(api, args.ref)
        updated = api.rename(found["id"], args.name, args.description)
        ok(f"Renamed to {updated.get('name')}")
        return 0
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
