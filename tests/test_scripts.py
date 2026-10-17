from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import _common, cleanup, doctor, logs, setup, status, up

ROOT = Path(__file__).resolve().parents[1]


def test_compose_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_common.COMPOSE_OVERRIDE_KEY, "podman compose")
    assert _common.detect_compose() == ("podman", "compose")


def test_compose_detection_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_common.COMPOSE_OVERRIDE_KEY, raising=False)
    monkeypatch.setattr(_common, "read_env", lambda *a: {})
    available = {("docker-compose",), ("podman", "compose")}
    monkeypatch.setattr(_common, "_works", lambda argv: tuple(argv) in available)
    assert _common.detect_compose() == ("docker-compose",)
    available.clear()
    assert _common.detect_compose() is None


@pytest.mark.parametrize(
    "text",
    [
        '{"Service": "mongodb", "State": "running", "Health": "healthy"}\n'
        '{"Service": "qdrant", "State": "running", "Health": ""}',
        json.dumps(
            [
                {"Service": "mongodb", "State": "running", "Health": "healthy"},
                {"Service": "qdrant", "State": "running", "Health": ""},
            ]
        ),
    ],
    ids=["docker-json-lines", "podman-json-array"],
)
def test_parse_ps_json_accepts_both_shapes(text: str) -> None:
    rows = _common.parse_ps_json(text)
    assert [r["Service"] for r in rows] == ["mongodb", "qdrant"]
    assert _common.parse_ps_json("  ") == []


def test_read_env_skips_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# c\n\nA=1\nB = two=2\nnot a pair\n", encoding="utf-8")
    assert _common.read_env(env) == {"A": "1", "B": "two=2"}
    assert _common.read_env(tmp_path / "missing") == {}


def test_render_env_fills_secrets_and_derives_urls() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    counter = iter(f"s{i}" for i in range(10))
    rendered = setup.render_env(template, make_secret=lambda: next(counter))
    lines = dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert "=CHANGE_ME" not in rendered
    assert lines["MONGODB_URI"] == (
        f"mongodb://ai_twin:{lines['MONGO_ROOT_PASSWORD']}@127.0.0.1:27018/?authSource=admin"
    )
    assert lines["REDIS_URL"] == f"redis://:{lines['REDIS_PASSWORD']}@127.0.0.1:6380/0"
    assert lines["QDRANT_URL"] == "http://127.0.0.1:6334"


def test_ensure_env_creates_once(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    target = tmp_path / ".env"
    assert setup.ensure_env(target, example) is True
    first = target.read_text(encoding="utf-8")
    assert setup.ensure_env(target, example) is False
    assert target.read_text(encoding="utf-8") == first


def test_cleanup_plan_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".ruff_cache").mkdir()
    (tmp_path / ".coverage").write_text("x")
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    args, caches = cleanup.plan(keep_data=False, root=tmp_path)
    assert "--volumes" in args
    assert len(caches) == 3
    assert "--volumes" not in cleanup.plan(keep_data=True, root=tmp_path)[0]

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(cleanup, "ROOT", tmp_path)
    monkeypatch.setattr(cleanup, "plan", lambda keep: (args, caches))
    monkeypatch.setattr(cleanup, "compose", lambda *a, **k: calls.append(a))

    assert cleanup.main([]) == 0
    assert calls == []
    assert all(p.exists() for p in caches)

    assert cleanup.main(["--apply"]) == 0
    assert calls == [tuple(args)]
    assert not any(p.exists() for p in caches)


def test_doctor_env_findings() -> None:
    example = {"A": "x", "B": "CHANGE_ME"}
    assert doctor.check_env({}, example)[0].level == "warn"
    levels = [f.level for f in doctor.check_env({"A": "CHANGE_ME"}, example)]
    assert levels == ["ok", "warn", "fail"]


def test_doctor_port_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "port_free", lambda port, host="127.0.0.1": port != 6380)
    findings = doctor.check_ports({}, stack_running=False)
    assert [f.level for f in findings] == ["ok", "warn", "ok", "ok"]
    assert all(f.level == "ok" for f in doctor.check_ports({}, stack_running=True))


def test_status_render_and_health_gate() -> None:
    rows = [
        {"service": "mongodb", "state": "running", "health": "healthy", "status": "Up"},
        {"service": "qdrant", "state": "running", "health": "-", "status": "Up"},
    ]
    text = status.render(rows)
    assert "mongodb" in text and "qdrant" in text
    assert up.all_healthy(rows)
    assert not up.all_healthy([{**rows[0], "health": "starting"}])
    assert not up.all_healthy([])


def test_logs_args() -> None:
    assert logs.build_args(None, False, 50) == ["logs", "--tail", "50"]
    assert logs.build_args("mongodb", True, 10) == ["logs", "--tail", "10", "--follow", "mongodb"]


def test_no_go_template_braces() -> None:
    paths = [ROOT / "Makefile", *(ROOT / "deploy").rglob("*"), *(ROOT / "scripts").rglob("*")]
    offenders = [
        str(p.relative_to(ROOT))
        for p in paths
        if p.is_file() and p.suffix != ".pyc" and "{" * 2 in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_resolve_uses_the_path_which_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = r"C:\Tools\docker-compose.CMD"
    monkeypatch.setattr("shutil.which", lambda name: shim if name == "docker-compose" else None)
    assert _common.resolve(["docker-compose", "up", "-d"]) == [shim, "up", "-d"]
    assert _common.resolve(["podman", "compose"]) is None


def test_compose_runs_the_resolved_program(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = r"C:\Tools\docker-compose.CMD"
    monkeypatch.setenv(_common.COMPOSE_OVERRIDE_KEY, "docker-compose")
    monkeypatch.setattr("shutil.which", lambda name: shim if name == "docker-compose" else None)
    seen: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: seen.append(argv))
    _common.compose("down")
    assert seen[0][0] == shim and seen[0][-1] == "down"


def test_missing_override_binary_fails_with_the_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(_common.COMPOSE_OVERRIDE_KEY, "podman compose")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        _common.require_compose()
    assert exc.value.code == 1
    err = capsys.readouterr()
    assert "'podman' is not on PATH" in err.err and "Docker Desktop" in err.out


def test_a_program_that_cannot_start_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def winerror_2(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr("subprocess.run", winerror_2)
    with pytest.raises(SystemExit) as exc:
        _common.run(["uv", "--version"])
    assert exc.value.code == 127
    assert "Could not start 'uv'" in capsys.readouterr().err


def test_quoted_env_values_are_unquoted(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('AI_TWIN_COMPOSE="docker compose"\nA=\'x\'\nB="half\nC="\n', encoding="utf-8")
    assert _common.read_env(env) == {
        "AI_TWIN_COMPOSE": "docker compose",
        "A": "x",
        "B": '"half',
        "C": '"',
    }


def test_quoted_compose_override_from_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Windows report: AI_TWIN_COMPOSE="docker compose" ran a program named `"docker`.
    monkeypatch.delenv(_common.COMPOSE_OVERRIDE_KEY, raising=False)
    monkeypatch.setattr(
        _common, "read_env", lambda *a: {_common.COMPOSE_OVERRIDE_KEY: "docker compose"}
    )
    assert _common.detect_compose() == ("docker", "compose")


def test_extensionless_windows_match_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    script = r"C:\Program Files\Docker\Docker\resources\bin\docker"
    monkeypatch.setattr("shutil.which", lambda name: script)
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert _common.resolve(["docker", "compose"]) is None
    monkeypatch.setattr("shutil.which", lambda name: script + ".exe")
    assert _common.resolve(["docker", "compose"]) == [script + ".exe", "compose"]
