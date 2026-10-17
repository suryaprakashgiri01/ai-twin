"""Print the local stack's containers: `python -m scripts.status`."""

from __future__ import annotations

from scripts._common import info, paint, service_states


def render(rows: list[dict[str, str]]) -> str:
    header = f"{'SERVICE':<10} {'STATE':<10} {'HEALTH':<10} STATUS"
    lines = [header]
    for row in rows:
        colour = (
            "green" if row["health"] in ("healthy", "-") and row["state"] == "running" else "yellow"
        )
        state = paint(f"{row['state']:<10}", colour)
        lines.append(f"{row['service']:<10} {state} {row['health']:<10} {row['status']}")
    return "\n".join(lines)


def main() -> int:
    rows = service_states()
    if not rows:
        info("No containers. Start the stack with: make up  (Windows: ./scripts/dev.ps1 up)")
        return 0
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
