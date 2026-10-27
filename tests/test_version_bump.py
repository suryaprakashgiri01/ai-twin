import re
from pathlib import Path

import pytest

import scripts.version_bump as vb
from scripts.version_bump import bump

_TOKEN = r'DEFAULT_VERSION = "'
_VERSION = re.compile(r'DEFAULT_VERSION = "(\d+\.\d+\.\d+)"')
_ORIG = (
    Path(vb.__file__)
    .resolve()
    .parent.parent.joinpath("app", "core", "config.py")
    .read_text(encoding="utf-8")
)


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a temp copy of config.py with a known version."""
    found = _VERSION.search(_ORIG)
    assert found is not None, "DEFAULT_VERSION missing from app/core/config.py"
    target = tmp_path / "config.py"
    target.write_text(
        _ORIG.replace(f'{_TOKEN}{found.group(1)}"', f'{_TOKEN}1.2.3"'), encoding="utf-8"
    )
    monkeypatch.setattr(vb, "CONFIG_PATH", target)
    return target


def test_default_version_is_present() -> None:
    assert 'DEFAULT_VERSION = "' in _ORIG


def test_relative_bumps() -> None:
    assert bump("1.2.3", "patch") == "1.2.4"
    assert bump("1.2.3", "minor") == "1.3.0"
    assert bump("1.2.3", "major") == "2.0.0"


def test_absolute_overrides(fake_config: Path) -> None:
    assert vb.rewrite("9.9.9")
    assert 'DEFAULT_VERSION = "9.9.9"' in fake_config.read_text(encoding="utf-8")


def test_unchanged_fails(fake_config: Path) -> None:
    assert not vb.rewrite("1.2.3")


def test_current_version(fake_config: Path) -> None:
    assert vb.current_version()[1] == "1.2.3"


def test_parse() -> None:
    assert vb.parse(["patch"]).step == "patch"
    assert vb.parse(["absolute", "2.0.0"]).version == "2.0.0"


def test_main_smoke(
    fake_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: main() rewrites the file and reports the change."""
    rc = vb.main(["patch"])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'DEFAULT_VERSION = "1.2.4"' in fake_config.read_text(encoding="utf-8")
    assert "1.2.3 -> 1.2.4" in out
