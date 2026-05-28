"""Test helpers for building isolated workspaces."""

from pathlib import Path

import yaml

from utils.config import Config


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_definition(
    base_path: Path,
    def_id: str,
    filename: str,
    frontmatter: dict,
    body: str,
) -> None:
    content = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n{body.strip()}\n"
    write_file(base_path / def_id / filename, content)


def make_workspace(
    root: Path,
    *,
    default_agent: str = "pickle",
    routing_bindings: list[dict] | None = None,
) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "skills").mkdir()

    config = {
        "llm": {
            "provider": "openai",
            "model": "test-model",
            "api_key": "test-key",
        },
        "default_agent": default_agent,
        "routing": {"bindings": routing_bindings or []},
    }
    write_file(workspace / "config.user.yaml", yaml.safe_dump(config, sort_keys=False))

    write_definition(
        workspace / "agents",
        "pickle",
        "AGENT.md",
        {"name": "Pickle", "description": "Default agent"},
        "You are Pickle.",
    )
    write_definition(
        workspace / "agents",
        "cookie",
        "AGENT.md",
        {"name": "Cookie", "description": "Memory agent"},
        "You are Cookie.",
    )

    return workspace


def make_context(workspace: Path):
    from core.context import SharedContext

    return SharedContext(Config.load(workspace), channels=[])
