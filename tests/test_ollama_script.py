"""local-ollama T1: the native Ollama script, against a mock HTTP API and a fake launcher."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import ollama


def api_handler(state: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        state.setdefault("paths", []).append(request.url.path)
        if not state.get("up"):
            raise httpx.ConnectError("refused")
        match request.url.path:
            case "/api/version":
                return httpx.Response(200, json={"version": "0.12.0"})
            case "/api/tags":
                return httpx.Response(200, json={"models": [{"name": n} for n in state["pulled"]]})
            case "/api/pull":
                body = json.loads(request.content)
                state["pulled"].append(body["model"])
                lines = [
                    {"status": "pulling"},
                    {"status": "pulling", "total": 10, "completed": 10},
                    {"status": "success"},
                ]
                return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))
            case "/api/chat":
                state["chat"] = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={"message": {"content": "ready"}, "eval_count": 2, "eval_duration": 1e8},
                )
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def target(url: str = "http://127.0.0.1:11434", model: str = "llama3.2:1b") -> ollama.Target:
    # One model for both entries keeps the scripted server's state to a single pull.
    env = {"OLLAMA_BASE_URL": url, "OLLAMA_MODEL": model, "OLLAMA_QUALITY_MODEL": model}
    env["OLLAMA_MAX_MODEL"] = model
    return ollama.target(env)


def test_target_and_model_defaults() -> None:
    t = ollama.target({})
    assert (t.url, t.host, t.port, t.loopback) == (
        "http://127.0.0.1:11434",
        "127.0.0.1",
        11434,
        True,
    )
    assert t.model == ollama.inventory_model() == "llama3.2:3b"
    assert t.all_models == ("llama3.2:3b", "qwen2.5:7b", "qwen2.5:14b")
    assert ollama.target({"OLLAMA_QUALITY_MODEL": "qwen2.5:3b"}).all_models == (
        "llama3.2:3b",
        "qwen2.5:3b",
        "qwen2.5:14b",
    )
    assert ollama.target({"OLLAMA_BASE_URL": "http://localhost:9999/"}).port == 9999
    with pytest.raises(ollama.OllamaError):
        ollama.target({"OLLAMA_BASE_URL": "not a url"})


def test_non_loopback_is_refused() -> None:
    remote = target("http://10.0.0.5:11434")
    assert not remote.loopback
    with pytest.raises(ollama.OllamaError, match="this machine"):
        ollama.require_loopback(remote)
    api = ollama.Api(remote, api_handler({"up": True, "pulled": []}))
    with pytest.raises(ollama.OllamaError):
        ollama.cmd_start(remote, api, launcher=lambda *a: pytest.fail("must not launch"))
    with pytest.raises(ollama.OllamaError):
        ollama.cmd_smoke(remote, api)


def test_has_model_matches_latest_tag() -> None:
    assert ollama.has_model(["llama3.2:1b"], "llama3.2:1b")
    assert ollama.has_model(["qwen:latest"], "qwen")
    assert not ollama.has_model(["llama3.2:3b"], "llama3.2:1b")


def test_launcher_and_log_uses_script_pid(tmp_path: Path) -> None:
    pid = tmp_path / "ollama.pid"
    pid.write_text("12345", encoding="utf-8")
    who, log = ollama.launcher_and_log(pid_file=pid)
    assert "script" in who
    assert log == ollama.LOG_FILE


def test_cmd_logs_tails_file(tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "server.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    pid = tmp_path / "ollama.pid"
    pid.write_text("1", encoding="utf-8")
    monkeypatch.setattr(ollama, "LOG_FILE", log)
    assert ollama.cmd_logs(lines=2, pid_file=pid) == 0
    out = capsys.readouterr().out
    assert "two" in out and "three" in out


def test_start_does_nothing_when_a_server_answers(tmp_path: Path) -> None:
    t = target()
    api = ollama.Api(t, api_handler({"up": True, "pulled": []}))
    pid_file = tmp_path / "ollama.pid"
    launched: list[Any] = []

    def launcher(*args: Any) -> int:
        launched.append(args)
        return 1

    assert ollama.cmd_start(t, api, launcher, pid_file=pid_file) == 0
    assert launched == [] and not pid_file.exists()


def test_start_launches_bound_to_the_configured_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"up": False, "pulled": []}
    t = target("http://127.0.0.1:11500")
    api = ollama.Api(t, api_handler(state))
    monkeypatch.setattr("scripts.ollama.shutil.which", lambda name: f"/usr/bin/{name}")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def launcher(argv: list[str], env: dict[str, str], log: Path) -> int:
        calls.append((argv, env))
        state["up"] = True
        return 4242

    pid_file = tmp_path / "run" / "ollama.pid"
    assert ollama.cmd_start(t, api, launcher, pid_file=pid_file) == 0
    assert calls == [(["/usr/bin/ollama", "serve"], {"OLLAMA_HOST": "127.0.0.1:11500"})]
    assert pid_file.read_text() == "4242"


def test_start_without_the_binary_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = target()
    monkeypatch.setattr("scripts.ollama.shutil.which", lambda name: None)
    api = ollama.Api(t, api_handler({"up": False, "pulled": []}))
    assert ollama.cmd_start(t, api, lambda *a: 1, pid_file=tmp_path / "p") == 1


def test_start_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = target()
    monkeypatch.setattr("scripts.ollama.shutil.which", lambda name: "/usr/bin/ollama")
    api = ollama.Api(t, api_handler({"up": False, "pulled": []}))
    assert ollama.cmd_start(t, api, lambda *a: 7, timeout=0.1, pid_file=tmp_path / "p") == 1


def test_stop_only_stops_what_start_launched(tmp_path: Path) -> None:
    pid_file = tmp_path / "ollama.pid"
    killed: list[tuple[int, int]] = []
    assert ollama.cmd_stop(pid_file, kill=lambda pid, sig: killed.append((pid, sig))) == 0
    assert killed == []  # no pid file: a desktop-app server is left alone
    pid_file.write_text("4242")
    assert ollama.cmd_stop(pid_file, kill=lambda pid, sig: killed.append((pid, sig))) == 0
    assert killed == [(4242, signal.SIGTERM)] and not pid_file.exists()

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    pid_file.write_text("1")
    assert ollama.cmd_stop(pid_file, kill=gone) == 0 and not pid_file.exists()


def test_status_pull_and_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    state: dict[str, Any] = {"up": True, "pulled": []}
    t = target()
    api = ollama.Api(t, api_handler(state))
    assert ollama.cmd_status(t, api) == 1  # model not pulled yet
    assert ollama.cmd_pull(t, api, None) == 0
    assert state["pulled"] == ["llama3.2:1b"]
    assert ollama.cmd_status(t, api) == 0
    assert ollama.cmd_smoke(t, api) == 0
    assert state["chat"]["messages"][0]["content"] == ollama.SMOKE_PROMPT
    out = capsys.readouterr().out
    assert "20 tok/s" in out and "'ready'" in out


def test_status_and_pull_when_down() -> None:
    t = target()
    api = ollama.Api(t, api_handler({"up": False, "pulled": []}))
    assert ollama.cmd_status(t, api) == 1
    assert ollama.cmd_pull(t, api, "x") == 1


def test_pull_error_is_reported() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        return httpx.Response(200, text=json.dumps({"error": "manifest unknown"}))

    t = target()
    with pytest.raises(ollama.OllamaError, match="manifest unknown"):
        ollama.cmd_pull(t, ollama.Api(t, httpx.MockTransport(handle)), "nope:1b")


def test_install_argv_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.ollama.shutil.which", lambda name: f"/bin/{name}")
    assert ollama.install_argv("Darwin") == ["brew", "install", "ollama"]
    assert ollama.install_argv("Windows") == ["winget", "install", "--id", "Ollama.Ollama", "-e"]
    assert ollama.install_argv("Linux") is None
    assert ollama.cmd_install() == 0  # "installed" already: nothing runs


def test_main_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "read_env", lambda: {"OLLAMA_BASE_URL": "http://10.1.1.1:11434"})
    assert ollama.main(["smoke"]) == 1
    monkeypatch.setattr(ollama, "read_env", lambda: {"OLLAMA_MODEL": "m"})
    assert ollama.main(["status"], transport=api_handler({"up": False, "pulled": []})) == 1


def test_doctor_reports_ollama() -> None:
    from scripts import doctor

    assert doctor.check_ollama({})[0].level == "ok"
    remote = doctor.check_ollama({"OLLAMA_BASE_URL": "http://10.0.0.5:11434", "OLLAMA_MODEL": "m"})
    assert remote[0].level == "fail"
    env = {"OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_MODEL": "llama3.2:1b"}
    env["OLLAMA_QUALITY_MODEL"] = env["OLLAMA_MAX_MODEL"] = "qwen2.5:7b"
    state: dict[str, Any] = {"up": False, "pulled": []}
    api = ollama.Api(ollama.target(env), api_handler(state))
    down = doctor.check_ollama(env, api)[0]
    assert down.level == "warn" and "start" in down.message
    state["up"] = True
    missing = doctor.check_ollama(env, api)[0]
    assert missing.level == "warn" and "pull" in missing.message
    state["pulled"].append("llama3.2:1b")
    only_qwen = doctor.check_ollama(env, api)
    assert len(only_qwen) == 1 and "qwen2.5:7b" in only_qwen[0].message
    state["pulled"].append("qwen2.5:7b")
    assert doctor.check_ollama(env, api)[0].level == "ok"
