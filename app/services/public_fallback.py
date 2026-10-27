"""The guaranteed public answer: the owner's public projects, from a curated file.

The published knowledge base is the primary source for a visitor's question. When it has
nothing usable (no passage survives the reranker, or the model decides it cannot answer),
the visitor still gets an answer: this module renders the owner's public project list and
the links to it, straight from `config/public_projects.yaml`.

The fallback is a fixed reply, not a model answer: it is built from owner-curated text, so
it is grounded in its own context (which is what the output guard's grounding judge sees)
and it cannot invent anything. Every use is logged as a `kb fallback` event, with the
intent and a fingerprint of the question, so the knowledge-base gap can be found and closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

MAX_PROJECTS = 20
MAX_SUMMARY_CHARS = 400


class PublicProject(BaseModel):
    """One public project: a name, its public repository and a one-line summary."""

    name: str
    repo: str
    summary: str
    url: str | None = None

    @property
    def link(self) -> str:
        return self.url or f"https://github.com/{self.repo}"


class PublicProjectIndex(BaseModel):
    """The owner's public work, as published."""

    portfolio_url: str = ""
    github_url: str = ""
    projects: list[PublicProject] = Field(default_factory=list, max_length=MAX_PROJECTS)


def load_index(path: Path) -> PublicProjectIndex:
    """Read the index; a missing or broken file means no fallback (never an error)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return PublicProjectIndex()
    try:
        data = yaml.safe_load(raw) or {}
        index = PublicProjectIndex.model_validate(data)
    except (ValueError, yaml.YAMLError):
        return PublicProjectIndex()
    return index.model_copy(
        update={
            "projects": [
                p.model_copy(update={"summary": p.summary.strip()[:MAX_SUMMARY_CHARS]})
                for p in index.projects
            ]
        }
    )


@dataclass(frozen=True)
class PublicFallback:
    """The answer to use when the knowledge base has nothing."""

    index: PublicProjectIndex

    @property
    def available(self) -> bool:
        return bool(self.index.projects)

    def answer(self) -> str:
        """The visitor-facing answer: what the projects are and where to see them."""
        listed = "; ".join(f"{p.name} is {p.summary}" for p in self.index.projects)
        text = f"My public work is on GitHub: {listed}."
        if self.index.github_url:
            text += f" You can browse every repository at {self.index.github_url}."
        if self.index.portfolio_url:
            text += f" My portfolio site is {self.index.portfolio_url}."
        return text

    def context(self) -> str:
        """The fallback answer's own support, so the grounding judge sees the facts."""
        blocks = [
            f"[{n}] {p.name} ({p.repo})\n{p.summary}\n{p.link}"
            for n, p in enumerate(self.index.projects, start=1)
        ]
        if self.index.github_url:
            blocks.append(f"GitHub profile: {self.index.github_url}")
        if self.index.portfolio_url:
            blocks.append(f"Portfolio site: {self.index.portfolio_url}")
        return "\n\n".join(blocks)
