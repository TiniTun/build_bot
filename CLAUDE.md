# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Approach
- Think before acting. Read existing files before writing code.
- Be concise in output but thorough in reasoning.
- Prefer editing over rewriting whole files.
- Do not re-read files you have already read unless the file may have changed.
- Skip files over 100KB unless explicitly required.
- Suggest running /cost when a session is running long to monitor cache ratio.
- Recommend starting a new session when switching to an unrelated task.
- Test your code before declaring done.
- No sycophantic openers or closing fluff.
- Keep solutions simple and direct.
- User instructions always override this file.

## Commands

```bash
# Run the CLI
python -m cli.main chat
python -m cli.main chat --agent pickle --workspace ./default_workspace

# Or via installed entrypoint
build-bot chat
build-bot chat --agent pickle --workspace ./default_workspace

# Lint
ruff check .
ruff format .
```

No test suite is configured yet.

## Architecture

**build-bot** is a personal AI assistant framework. The core idea: a user types a message → `InboundEvent` is published to `EventBus` → `AgentWorker` picks it up → runs `AgentSession.chat()` → publishes `OutboundEvent` back → CLI renders the response.

### Key layers

**`core/`** — domain logic, no I/O
- `SharedContext` — single shared-state object passed everywhere. Holds `history_store`, `agent_loader`, `skill_loader`, `command_registry`, `eventbus`.
- `Agent` — creates/resumes `AgentSession`s. Reads agent definitions from the workspace.
- `AgentSession` — orchestrates the LLM loop: send messages, handle tool calls, loop until no more tool calls.
- `SessionState` — pure dataclass holding `messages: list[Message]` and a reference to `SharedContext`. Replaces itself when compacted.
- `ContextGuard` — monitors token usage; triggers compaction when threshold is hit.
- `EventBus` — async pub/sub queue. Lives in `core/` but extends `server.Worker`.
- `core/commands/` — slash-command subsystem (`/help`, `/compact`, `/context`, `/session`, `/skills`). `CommandRegistry` is on `SharedContext`, not on `AgentSession`.

**`server/`**
- `Worker` / `SubscriberWorker` — base async lifecycle classes.
- `AgentWorker` — subscribes to `InboundEvent`; for each event resolves the agent, creates/resumes a session, checks for slash commands first, then calls `session.chat()`.

**`provider/`** — thin adapters
- `provider/llm/` — LiteLLM wrapper.
- `provider/web_search/` — Brave Search.
- `provider/web_read/` — Crawl4AI.

**`tools/`** — LLM-callable tools
- `ToolRegistry` — register/execute tools by name.
- Tools are built per-session in `Agent._build_tools()`.
- Slash-command handlers must access shared services via `session.shared_context`, not directly on `session` or `session.agent`.

**`utils/`**
- `Config` — Pydantic model loaded from `<workspace>/config.user.yaml`.
- `def_loader` — parses YAML-frontmatter Markdown definitions (agents, skills).

**`cli/`** — Typer app. `ChatLoop` wires `EventBus` + `AgentWorker`, holds a `response_queue` to bridge async events back to the blocking prompt.

### Workspace layout

A workspace is a directory containing:
```
config.user.yaml    # LLM keys, default_agent, optional websearch/webread
agents/<id>/AGENT.md  # YAML frontmatter (name, llm overrides, allow_skills) + system prompt body
skills/<id>/SKILL.md  # YAML frontmatter (name, description) + skill content
```

`Config.load(workspace_path)` resolves all relative paths (`agents_path`, `skills_path`, `history_path`) against the workspace root. Default workspace is `./default_workspace`.

### Slash-command handler notes

- Handlers should access shared services via `session.shared_context`, not directly on `session` or `session.agent`.
- `AgentLoader.load()` raises `DefNotFoundError` for missing agents; `/agent <id>` and `/route <pattern> <agent_id>` should catch that instead of `ValueError`.
