from __future__ import annotations

from app import app, root_agent


def main() -> None:
    print(f"Autonomous Execution Agent initialized (name={root_agent.name}, model={root_agent.model})")


if __name__ == "__main__":
    main()
