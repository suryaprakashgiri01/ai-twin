"""Graphify wrappers: `python -m scripts.graph update|query|explain|path …`.

Agents and owners use the same make / PowerShell targets. When graphify is not on PATH,
the command exits 1 with an install hint (never a traceback).
"""

from __future__ import annotations

import argparse
import shutil

from scripts._common import fail, info, ok, run

INSTALL_HINT = (
    "graphify not found on PATH. Install the CLI used for this repo's code graph, then retry."
)


def graphify_bin() -> str | None:
    return shutil.which("graphify")


def run_graphify(argv: list[str]) -> int:
    binary = graphify_bin()
    if binary is None:
        fail(INSTALL_HINT)
        return 1
    result = run([binary, *argv], check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("update", help="Rebuild the AST graph for this repo (graphify update .)")

    query = sub.add_parser("query", help="Ask a natural-language question of the graph")
    query.add_argument("text", nargs="+", help="question text")

    explain = sub.add_parser("explain", help="Explain a concept from the graph")
    explain.add_argument("text", nargs="+", help="concept")

    path = sub.add_parser("path", help="Shortest path between two symbols")
    path.add_argument("a", help="start symbol")
    path.add_argument("b", help="end symbol")

    args = parser.parse_args(argv)
    if graphify_bin() is None:
        fail(INSTALL_HINT)
        return 1
    match args.command:
        case "update":
            info("graphify update .")
            code = run_graphify(["update", "."])
            if code == 0:
                ok("graph updated")
            return code
        case "query":
            return run_graphify(["query", " ".join(args.text)])
        case "explain":
            return run_graphify(["explain", " ".join(args.text)])
        case "path":
            return run_graphify(["path", args.a, args.b])
        case _:
            fail(f"unknown command: {args.command}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
