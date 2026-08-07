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
uv run build-bot --workspace ./default_workspace chat --agent pickle

# Run the server workers
uv run build-bot server
uv run build-bot --workspace ./default_workspace server

# Or via installed entrypoint
build-bot chat
build-bot --workspace ./default_workspace chat --agent pickle
build-bot server

# Validate workspace skills
uv run build-bot --workspace ./default_workspace validate-skills

# Tests
uv run python -m unittest discover -s tests -p 'test_*.py'

# Lint
uv run ruff check .
uv run ruff format .
```

- Tests use `unittest` under `tests/`; keep new coverage narrow and behavior-focused.
- If local `uv` is blocked by sandbox/cache/network issues, use the Codex `Test Run` environment action on `bakeryd`.
- `--workspace` is a Typer global option, so it must come before the subcommand.
- Prefer `uv run ruff check <files>` when unrelated repo-wide lint noise would hide the signal.

## Architecture

**build-bot** is a personal AI assistant framework. The core idea: input from CLI, WebSocket, Telegram, cron, or agent dispatch becomes an event on `EventBus`; workers route it to an agent session and publish delivery or dispatch-result events back.

### Key layers

**`core/`** — domain logic, no I/O
- `SharedContext` — single shared-state object passed everywhere. Holds `history_store`, `agent_loader`, `skill_loader`, `command_registry`, `eventbus`, `mcp_hub`.
- `Agent` — creates/resumes `AgentSession`s. Reads agent definitions from the workspace.
- `AgentSession` — orchestrates the LLM loop: send messages, handle tool calls, loop until no more tool calls.
- `SessionState` — pure dataclass holding `messages: list[Message]` and a reference to `SharedContext`. Replaces itself when compacted.
- `ContextGuard` — monitors token usage; triggers compaction when threshold is hit.
- `EventBus` — async pub/sub queue. Lives in `core/` but extends `server.Worker`.
- `core/commands/` — slash-command subsystem (`/help`, `/compact`, `/context`, `/session`, `/skills`, `/mcp`). `CommandRegistry` is on `SharedContext`, not on `AgentSession`.
- `RoutingTable` — resolves source strings to agents, caches source-session affinity in runtime config, and clears stale cached sessions missing from history.
- `CronLoader` — loads `<crons_path>/<cron-id>/CRON.md` definitions and validates cron schedules.
- `SkillLoader` — Skills v2 loader/validator; invalid skills are logged and skipped during discovery, while `validate-skills` surfaces errors.
- `PendingActionStore` — stores confirmation-required actions under `<event_path>/pending_actions`.

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
- `provider/mcp/` — MCP client layer. `client.py` owns the only import of the MCP SDK; `models.py`/`errors.py` are the SDK-free vocabulary; `hub.py` is the process-level `McpHub`.

**`tools/`** — LLM-callable tools
- `ToolRegistry` — register/execute tools by name; the dispatch parameter is `tool_name` so tools may safely accept their own `name` argument.
- `ToolResult.success()` returns plain text; `ToolResult.error()` returns JSON with `ok: false` and a stable `error` object.
- `ToolResult.requires_confirmation()` returns JSON with `requires_confirmation: true` and a pending action payload.
- `ToolErrorCode` includes `permission_denied`, `auth_missing`, `rate_limited`, `invalid_args`, `not_found`, `provider_error`.
- Built-ins are `read`, `write`, `edit`, `create_cron_job`, and `bash`.
- `create_cron_job` is the supported path for creating `CRON.md` files. It writes under configured `crons_path`, validates the agent, accepts `name` plus compatible `title`, and supports `run_at` for one-off jobs.
- Tools are built per-session in `Agent._build_tools()` through `CapabilityRegistry` + `ToolPolicy`.
- Capability IDs are dotted (`email.search`); LLM tool names are safe underscores (`email_search`). Web tools keep legacy names `websearch` / `webread`.
- Slash-command handlers must access shared services via `session.shared_context`, not directly on `session` or `session.agent`.
- `email_*` and `calendar_*` tools are hidden unless `external_tools.<domain>.enabled` and policy enable their capability; null providers return `auth_missing`.
- `ToolRegistry.register` raises `ToolNameCollisionError` on a duplicate visible name; it is never last-write-wins.

### MCP (remote tools)

- `McpHub` on `SharedContext` owns all connections. Its `start()`/`stop()` are awaited by `cli/chat.py` and `server/server.py` — before workers start and after they stop.
- Startup order per server is fixed: `/health` → initialize → `list_tools` → publish snapshot. Nothing is exposed before discovery completes.
- Capability id `mcp.<server_id>.<tool>`; LLM name `mcp_<server_id>_<tool>` (sanitized, truncated, hash-suffixed on collision).
- Discovery is fail-closed in `tools/mcp_tools.py`, not in `ToolPolicy`, so the allowlist holds even in permissive legacy mode. A tool the server adds later stays quarantined.
- Server annotations/descriptions/instructions are untrusted; they never grant access or bypass confirmation. `use_server_instructions` is opt-in, per-agent, and size-capped.
- Sessions get stable tool schemas; `McpTool.execute` re-checks `hub.is_tool_available` at call time.
- No call is ever retried automatically. A timeout is ambiguous and is reported as non-retryable.
- Bearer tokens come from `token_env` only, are attached to every request in the session, and are redacted from every error, log, and `/mcp` output.
- Config hot-reload reaches the hub through `Config.add_reload_listener` → `McpHub.request_reconcile()`, which only signals the loop from the watchdog thread.
- Tests use `tests/mcp_fakes.py`: `FakeMcpHttpServer` (real SDK over `httpx2.MockTransport`) for protocol facts, `FakeMcpClient` for lifecycle and policy.

**`utils/`**
- `Config` — Pydantic model merged from `<workspace>/config.user.yaml` and optional `config.runtime.yaml`.
- Runtime config stores mutable state such as routing bindings, `sources`, and default delivery source.
- `tools.enabled_capabilities` is strict when configured; no `tools:` block means permissive legacy behavior.
- `external_tools.email` and `external_tools.calendar` are disabled by default.
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
- Confirm-required tools write pending actions; `/confirm <id>` executes through a permissive registry, `/reject <id>` discards.

### Skills v2

- Every `SKILL.md` needs `name`, `description`, non-empty `when_to_use`, and a non-empty body.
- Optional skill fields: `required_tools`, `permissions`, `references`, `scripts`; unknown frontmatter keys are rejected.
- Reference/script paths must be relative, stay inside the skill directory, and exist on disk.
- The `skill` tool exposes only metadata in its schema, then returns the body plus a manifest of references/scripts on invocation.
- `permissions` and `required_tools` are metadata only for now; they are not enforced.

### Capability policy

- Risk levels: `read`, `draft`, `confirm_required`, `write`.
- If `tools:` is absent, all registered capabilities are allowed and confirm gates are bypassed to preserve legacy behavior.
- If `tools.enabled_capabilities` is present, only listed dotted capability IDs are available.
- `confirm_required` defaults to confirmation when policy is configured; keep cron/post_message legacy-safe in permissive mode.
- `calendar_create_event` self-guards by recording a pending action and never mutating on first call.

### Workspace layout

A workspace is a directory containing:
```
config.user.yaml      # LLM keys, default_agent, optional websearch/webread/channels/api/tools/external_tools/mcp
config.runtime.yaml   # mutable runtime state; generated/updated by the app
agents/<id>/AGENT.md  # YAML frontmatter (name, llm overrides, allow_skills) + system prompt body
skills/<id>/SKILL.md  # Skills v2 frontmatter + body
crons/<id>/CRON.md    # YAML frontmatter + prompt body for scheduled jobs
.history/             # persisted sessions; source of truth for session existence
.event/               # persisted pending events
.event/pending_actions/ # confirmation-required tool actions
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
