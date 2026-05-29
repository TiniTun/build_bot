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
uv run build-bot chat
uv run build-bot chat --agent pickle --workspace ./default_workspace

# Run the server workers
uv run build-bot server

# Or via installed entrypoint
build-bot chat
build-bot chat --agent pickle --workspace ./default_workspace
build-bot server

# Tests
uv run python -m unittest discover -s tests -p 'test_*.py'

# Lint
ruff check .
ruff format .
```

- Tests use `unittest` under `tests/`; keep new coverage narrow and behavior-focused.
- If local `uv` is blocked by sandbox/cache/network issues, use the Codex `Test Run` environment action on `bakeryd`.
- Prefer path-scoped `ruff check <files>` when unrelated repo-wide lint noise would hide the signal.

## Architecture

**build-bot** is a personal AI assistant framework. The core idea: input from CLI, WebSocket, Telegram, cron, or agent dispatch becomes an event on `EventBus`; workers route it to an agent session and publish delivery or dispatch-result events back.

### Key layers

**`core/`** — domain logic, no I/O
- `SharedContext` — single shared-state object passed everywhere. Holds `history_store`, `agent_loader`, `skill_loader`, `command_registry`, `eventbus`.
- `Agent` — creates/resumes `AgentSession`s. Reads agent definitions from the workspace.
- `AgentSession` — orchestrates the LLM loop: send messages, handle tool calls, loop until no more tool calls.
- `SessionState` — pure dataclass holding `messages: list[Message]` and a reference to `SharedContext`. Replaces itself when compacted.
- `ContextGuard` — monitors token usage; triggers compaction when threshold is hit.
- `EventBus` — async pub/sub queue. Lives in `core/` but extends `server.Worker`.
- `core/commands/` — slash-command subsystem (`/help`, `/compact`, `/context`, `/session`, `/skills`). `CommandRegistry` is on `SharedContext`, not on `AgentSession`.
- `RoutingTable` — resolves source strings to agents, caches source-session affinity in runtime config, and clears stale cached sessions missing from history.
- `CronLoader` — loads `<crons_path>/<cron-id>/CRON.md` definitions and validates cron schedules.

**`server/`**
- `Worker` / `SubscriberWorker` — base async lifecycle classes.
- `AgentWorker` — subscribes to `InboundEvent` and `DispatchEvent`; resolves the session's agent, checks slash commands first, then calls `session.chat()`.
- `CronWorker` — scans loaded cron definitions every minute, creates cron sessions, publishes `DispatchEvent`, and deletes one-off jobs after dispatch.
- `DeliveryWorker` — delivers `OutboundEvent`s to platform channels, chunks messages by platform limit, retries with backoff, and acks empty non-error messages.
- `ChannelWorker` / Telegram channel — bridge platform messages into typed events.
- `WebSocketWorker` / `server/app.py` — FastAPI WebSocket endpoint at `/ws`.

**`provider/`** — thin adapters
- `provider/llm/` — LiteLLM wrapper.
- `provider/web_search/` — Brave Search.
- `provider/web_read/` — Crawl4AI.

**`tools/`** — LLM-callable tools
- `ToolRegistry` — register/execute tools by name; the dispatch parameter is `tool_name` so tools may safely accept their own `name` argument.
- `ToolResult.success()` returns plain text; `ToolResult.error()` returns JSON with `ok: false` and a stable `error` object.
- `ToolErrorCode` includes `permission_denied`, `auth_missing`, `rate_limited`, `invalid_args`, `not_found`, `provider_error`.
- Built-ins are `read`, `write`, `edit`, `create_cron_job`, and `bash`.
- `create_cron_job` is the supported path for creating `CRON.md` files. It writes under configured `crons_path`, validates the agent, accepts `name` plus compatible `title`, and supports `run_at` for one-off jobs.
- Tools are built per-session in `Agent._build_tools()`.
- Slash-command handlers must access shared services via `session.shared_context`, not directly on `session` or `session.agent`.

**`utils/`**
- `Config` — Pydantic model merged from `<workspace>/config.user.yaml` and optional `config.runtime.yaml`.
- Runtime config stores mutable state such as routing bindings and `sources`.
- Relative paths resolve against the workspace root: agents, skills, crons, memories, logs, history, events.
- `timezone` must be a valid IANA timezone when configured and is used by cron scheduling helpers.
- `def_loader` — parses YAML-frontmatter Markdown definitions (agents, skills).

**`cli/`** — Typer app. `chat` wires `EventBus` + `AgentWorker`; `server` starts the long-running worker stack.

**`channel/`**
- Channels expose platform-specific reply/ingress behavior. Telegram is configured through workspace config.

### Events and delivery

- Use typed `EventSource`s (`platform-cli`, `platform-ws`, `platform-telegram`, `cron`, `agent`) instead of raw strings.
- `InboundEvent` is external work entering the system; `OutboundEvent` is platform delivery; `DispatchEvent` / `DispatchResultEvent` are internal agent-to-agent work.
- Cron dispatches should avoid emitting empty final outbound messages; delivery should skip and ack empty non-error content.
- For Telegram routing bugs, verify source-session mappings against `.history/index.jsonl`, not only `config.runtime.yaml`.

### Workspace layout

A workspace is a directory containing:
```
config.user.yaml      # LLM keys, default_agent, optional websearch/webread/channels/api
config.runtime.yaml   # mutable runtime state; generated/updated by the app
agents/<id>/AGENT.md  # YAML frontmatter (name, llm overrides, allow_skills) + system prompt body
skills/<id>/SKILL.md  # YAML frontmatter (name, description) + skill content
crons/<id>/CRON.md    # YAML frontmatter + prompt body for scheduled jobs
.history/             # persisted sessions; source of truth for session existence
.event/               # persisted pending events
```

`Config.load(workspace_path)` resolves all relative paths (`agents_path`, `skills_path`, `history_path`) against the workspace root. Default workspace is `./default_workspace`.

### Cron notes

- Cron definitions live only under `config.crons_path`; do not hand-write cron files elsewhere.
- For one-off jobs, prefer `run_at` with an explicit UTC offset over manual UTC-to-cron conversion.
- `CronWorker` evaluates schedules in configured local timezone when `timezone` is set.
- `CronDef` enforces valid 5-field cron expressions and a minimum 5-minute granularity.

### Slash-command handler notes

- Handlers should access shared services via `session.shared_context`, not directly on `session` or `session.agent`.
- `AgentLoader.load()` raises `DefNotFoundError` for missing agents; `/agent <id>` and `/route <pattern> <agent_id>` should catch that instead of `ValueError`.
