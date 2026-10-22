"""A Markdown digest of a local git repository and one author's work in it.

`make kb-sync` uses it for manifest entries of kind `local_repo`. It covers repositories that
must never leave the machine (an employer's): nothing is fetched or sent anywhere, the digest
is written into the git-ignored vault, and the manifest marks it confidential.

The digest holds the README, the Markdown docs, languages and layout, and the author's own
commits (count, dates, subjects, the areas they changed most). It is capped in size so one
repository never floods the knowledge base.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

README_CHARS = 8000
DOC_CHARS = 2500
DOCS_TOTAL_CHARS = 20_000
MAX_SUBJECTS = 300
MAX_AREAS = 15
SKIP_DIRS = frozenset({".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__"})
LANGUAGES = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".cs": "C#",
    ".sql": "SQL",
    ".tf": "Terraform",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".sh": "Shell",
}


class DigestError(Exception):
    """The path is not a git repository, or git failed; the message says which."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DigestError(f"git {' '.join(args[:2])} failed in {repo}") from exc
    return result.stdout


def _files(repo: Path) -> list[Path]:
    tracked = _git(repo, "ls-files").splitlines()
    return [Path(p) for p in tracked if not SKIP_DIRS.intersection(Path(p).parts)]


def _languages(files: list[Path]) -> str:
    counts = Counter(LANGUAGES[f.suffix] for f in files if f.suffix in LANGUAGES)
    return ", ".join(f"{name} ({n})" for name, n in counts.most_common(8)) or "-"


def _layout(files: list[Path]) -> str:
    tops = Counter(f.parts[0] for f in files if len(f.parts) > 1)
    return ", ".join(f"`{d}/` ({n})" for d, n in tops.most_common(12)) or "-"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n…(cut)"


def _docs(repo: Path, files: list[Path]) -> list[str]:
    docs = sorted(f for f in files if f.suffix.lower() == ".md" and f.name.lower() != "readme.md")
    blocks: list[str] = []
    used = 0
    for doc in docs:
        if used >= DOCS_TOTAL_CHARS:
            blocks.append(f"(and {len(docs) - len(blocks)} more Markdown files)")
            break
        text = (repo / doc).read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        piece = _clip(text, DOC_CHARS)
        blocks.append(f"### {doc}\n\n{piece}")
        used += len(piece)
    return blocks


def _contributions(repo: Path, author: str) -> list[str]:
    log = _git(
        repo, "log", "--all", "-i", f"--author={author}", "--no-merges", "--format=%as\t%s"
    ).splitlines()
    if not log:
        return [f"No commits by {author} in this repository."]
    dates = sorted(line.split("\t", 1)[0] for line in log)
    subjects = list(dict.fromkeys(line.split("\t", 1)[1] for line in log if "\t" in line))
    touched = _git(
        repo,
        "log",
        "--all",
        "-i",
        f"--author={author}",
        "--no-merges",
        "--name-only",
        "--format=",
    ).splitlines()
    areas = Counter("/".join(Path(p).parts[:2]) for p in touched if p.strip())
    lines = [
        f"- Commits: {len(log)} (non-merge), from {dates[0]} to {dates[-1]}",
        "- Areas changed most: "
        + ", ".join(f"`{a}` ({n})" for a, n in areas.most_common(MAX_AREAS)),
        "",
        "Commit subjects, newest first:",
        "",
        *(f"- {s}" for s in subjects[:MAX_SUBJECTS]),
    ]
    if len(subjects) > MAX_SUBJECTS:
        lines.append(f"- …and {len(subjects) - MAX_SUBJECTS} more")
    return lines


def digest(repo: Path, author: str, title: str | None = None) -> str:
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise DigestError(f"{repo} is not a git repository")
    files = _files(repo)
    readme = next((f for f in files if len(f.parts) == 1 and f.name.lower() == "readme.md"), None)
    name = title or repo.name
    parts = [
        f"# {name} (repository I worked on)",
        "",
        f"- Languages (files): {_languages(files)}",
        f"- Layout: {_layout(files)}",
        f"- Tracked files: {len(files)}",
        "",
        "## My contributions",
        "",
        *_contributions(repo, author),
    ]
    if readme is not None:
        text = (repo / readme).read_text(encoding="utf-8", errors="replace").strip()
        parts += ["", "## README", "", _clip(text, README_CHARS)]
    docs = _docs(repo, files)
    if docs:
        parts += ["", "## Docs", "", *docs]
    return "\n".join(parts) + "\n"
