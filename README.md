# build-bot

**A self-hosted AI assistant that you define in Markdown and use from the terminal, Telegram, or your own application.**

build-bot turns an LLM into a long-running personal assistant: it keeps conversation history, routes requests between specialized agents, uses external services through tools, remembers durable context, and runs scheduled jobs. Agents, skills, and automations live in ordinary files inside a workspace, so the assistant remains inspectable and easy to customize.

The included workspace is not an empty demo. It contains a user-facing coordinator and specialists for memory, web research, Gmail, Google Calendar, Todoist, nearby-place search, and a movie library connected over MCP.

## What it can do

- Run with any model supported by [LiteLLM](https://docs.litellm.ai/docs/providers), including OpenAI-compatible endpoints and Anthropic models.
- Receive requests from CLI, Telegram, and WebSocket clients.
- Handle Telegram text, voice messages, shared locations, and confirmation buttons.
- Split work between multiple agents with different prompts, models, tools, and permissions.
- Search and read the web, work with files, execute shell commands, and run skill-owned scripts.
- Search Gmail, prepare replies, manage Google Calendar, work with Todoist, and find places with Google Places.
- Connect remote MCP servers and expose only explicitly approved tools to selected agents.
- Keep persistent conversation history and durable Markdown memory.
- Run recurring jobs and one-off reminders in a configured timezone.
- Require explicit confirmation before sensitive operations such as sending or deleting email, changing calendar events, or deleting tasks.
- Reload most workspace configuration without restarting the process.

## How messages reach the assistant

Every entry point is normalized into the same event pipeline:

```text
CLI / Telegram / WebSocket / cron
                ↓
             EventBus
                ↓
       routing + session history
                ↓
        agent → tools / specialists
                ↓
      reply to the originating channel
```

| Channel | Best suited for | Supported input |
| --- | --- | --- |
| **CLI** | Local setup, testing, and private interactive use | Text and slash commands |
| **Telegram** | An always-available personal assistant | Text, voice notes, locations, confirmation buttons |
| **WebSocket** | Web/mobile clients and integration into another product | JSON messages and streamed events |
| **Cron** | Background work, recurring checks, and reminders | Scheduled prompts dispatched to an agent |

Sessions are associated with their source, so a Telegram chat and a WebSocket client keep separate histories. Routing rules can send different users or source patterns to different agents.

## Included real-world setup

The default workspace uses **Pickle** as the only user-facing agent. Pickle delegates focused work to specialists and combines their results into one reply.

| Agent | Responsibility |
| --- | --- |
| `pickle` | User conversation, orchestration, reminders, and final answers |
| `cookie` | Durable facts, preferences, project context, decisions, and daily notes |
| `mail-assistant` | Gmail search, reading, triage, and reply drafting |
| `calendar-assistant` | Agenda, availability, conflicts, meeting preparation, and event changes |
| `task-assistant` | Todoist capture, lists, updates, completion, and guarded destructive actions |
| `researcher` | Web research, source comparison, and nearby-place discovery |
| `movie-assistant` | Movie-library search, viewing history, recommendations, and feedback through a remote MCP server |

Examples of requests supported by this workspace after the corresponding providers are configured:

```text
Summarize my unread email and draft replies where the answer is obvious.

What meetings do I have tomorrow, and are there any conflicts?

Add "send the proposal Friday p1 #Work" to Todoist.

Find three well-reviewed breakfast cafés within 3 km of this Telegram location.

Remember that I prefer meetings after 10:00.

Remind me tomorrow at 08:30 to take the documents.

Compare the current options for X and cite the sources.

Recommend a movie based on my viewing history and explain why it fits.
```

The assistant can combine these capabilities. For example, it can retrieve a stored scheduling preference, ask the calendar specialist for availability, prepare an event change, and wait for `/confirm <action_id>` before applying it.

## Quick start

### Requirements

- An API key for the LLM provider you want to use
- For a local installation: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/)
- Alternatively: Docker Engine with Docker Compose
- Optional: `ffmpeg` for Telegram voice messages

Clone the project and install its dependencies:

```bash
git clone https://github.com/TiniTun/build_bot.git
cd build_bot
uv sync
```

Create your local configuration:

```bash
cp default_workspace/config.example.yaml default_workspace/config.user.yaml
```

At minimum, set the model and API key in `default_workspace/config.user.yaml`:

```yaml
llm:
  provider: openai
  model: gpt-4.1
  api_key: sk-...
  temperature: 0.7
  max_tokens: 4096

default_agent: pickle
timezone: Australia/Brisbane
```

`provider` is a descriptive label; LiteLLM selects the actual backend from `model`. A custom OpenAI-compatible endpoint can be set with `api_base`. Individual agents may override the global model and generation settings in their `AGENT.md` frontmatter.

To use GPT-5.6 reasoning with function tools, select the explicit LiteLLM Responses transport:

```yaml
llm:
  provider: openai
  model: gpt-5.6-sol
  api_key: sk-...
  api_mode: responses
  reasoning_effort: medium
  store: true
  max_tokens: 8192
```

`api_mode` defaults to `chat_completions` for existing configurations. Responses calls use
`litellm.aresponses`, map `max_tokens` to `max_output_tokens` (reasoning and visible text
share this budget), and send function results using their `call_id`. Tools still execute
locally through the same capability and confirmation policies, sequentially.

This first Responses implementation requires explicit `store: true`: the API retains
response state and local session metadata remembers its continuation ID. Each request
sends fresh instructions and only the messages not yet acknowledged by that ID.
Local JSONL history remains readable, including sessions created before this change.
Changing the model or endpoint, repairing interrupted tool history, or compacting the
conversation starts a fresh chain from local history. Stateless encrypted-reasoning
replay (`store: false`) is not implemented.

Agents using another backend can override `api_mode: chat_completions` in their `llm`
frontmatter. Set `reasoning_effort: null` when that backend should not inherit an OpenAI
reasoning setting. Unsupported configurations fail explicitly; the provider does not
silently switch APIs or disable reasoning. Traces identify the API mode, response ID,
status and token usage; reasoning payloads are excluded.

Start a local chat:

```bash
uv run build-bot chat
```

Use another workspace or agent:

```bash
uv run build-bot --workspace ./my-workspace chat --agent my-agent
```

`--workspace` is a global option and must appear before the subcommand.

## Running channels

### CLI

The CLI is the smallest working setup and does not require the long-running server:

```bash
uv run build-bot --workspace ./default_workspace chat
```

Type `/help` in a conversation to see the available commands. Useful commands include `/agent`, `/skills`, `/crons`, `/mcp`, `/session`, `/context`, `/clear`, `/route`, `/bindings`, `/confirm`, and `/reject`.

### Telegram

Create a bot with [BotFather](https://t.me/BotFather), then add the channel configuration:

```yaml
channels:
  enabled: true
  telegram:
    bot_token: "123456:replace-me"
    allowed_user_ids:
      - "123456789"
```

`allowed_user_ids` should be set for a private bot. If it is empty, messages from every Telegram user are accepted.

Run the server:

```bash
uv run build-bot --workspace ./default_workspace server
```

Voice notes are optional and require `ffmpeg` on `PATH`:

```yaml
channels:
  enabled: true
  telegram:
    bot_token: "123456:replace-me"
    allowed_user_ids: ["123456789"]
    voice:
      enabled: true
      provider: openai
      model: gpt-4o-mini-transcribe
      api_key: null       # reuse llm.api_key
      max_file_size_mb: 20
      max_duration_seconds: 300
```

Telegram locations are converted to coordinates before reaching the agent. The included researcher can pass those coordinates to Google Places instead of guessing where “nearby” means.

### WebSocket

The server exposes `ws://127.0.0.1:8005/ws` by default. Send a JSON object with a stable client identifier and message content:

```json
{
  "source": "web-client-42",
  "content": "What do I have planned today?"
}
```

The connection receives typed event objects. An assistant reply has `"type": "OutboundEvent"`; use its `source`, `session_id`, and `content` fields to associate it with the client conversation.

To listen outside the host, change the API bind address:

```yaml
api:
  host: 0.0.0.0
  port: 8005
```

The WebSocket endpoint does not currently implement authentication. Keep it on loopback or put it behind an authenticated reverse proxy; do not expose port `8005` directly to the public internet.

### Scheduled jobs and proactive messages

Scheduled jobs are Markdown definitions under `crons/`. The supported creation path is the `create_cron_job` tool, used by the included `cron-ops` skill. Jobs use standard five-field cron expressions with a minimum interval of five minutes; one-off reminders are removed after they run.

Set a timezone so human times are interpreted consistently:

```yaml
timezone: Australia/Brisbane
```

For a cron agent to proactively deliver results, configure a platform destination. A Telegram source has this form:

```yaml
default_delivery_source: "platform-telegram:123456789:123456789"
```

The two values are the Telegram user ID and chat ID. Keep the Telegram channel enabled as shown above, and add `messaging.post_message` to the cron agent's `allowed_capabilities` in its `AGENT.md`.

## Tools and integrations

Tools are model-callable operations. A capability catalog describes each tool, its risk level, and its configuration requirements. The final tool set is the intersection of:

1. tools registered by the application;
2. the optional global `tools.enabled_capabilities` allowlist;
3. the agent's `allowed_capabilities` list.

This lets a coordinator delegate work without giving every specialist filesystem or shell access.

| Area | What the assistant can use |
| --- | --- |
| **Core** | Read, write, and edit files; execute shell commands; create scheduled jobs |
| **Skills** | Load task-specific instructions and run scripts declared by a skill |
| **Agents** | Dispatch bounded work to another agent and collect its result |
| **Web** | Brave web search and Crawl4AI page extraction |
| **Memory** | Search and store facts, preferences, project context, decisions, and daily notes |
| **Email** | Gmail search/read, draft replies, and confirmation-gated send/delete |
| **Calendar** | Google Calendar search/availability and confirmation-gated create/update/delete |
| **Tasks** | Todoist capture, lists, search, update, completion, and guarded delete/bulk operations |
| **Places** | Google Places nearby search with ratings, review evidence, distance, and map links |
| **MCP** | Remote tools discovered over Streamable HTTP and admitted through an exact allowlist |
| **Messaging** | Send a cron or background result to the configured platform |

Provider-backed tools only appear when their configuration is enabled. The full commented reference is in [`default_workspace/config.example.yaml`](default_workspace/config.example.yaml).

### Capability policy

Omitting the `tools` block keeps legacy permissive behavior, while per-agent `allowed_capabilities` still narrows access. To enforce a global allowlist and confirmation policy, list every capability your agents need:

```yaml
tools:
  enabled_capabilities:
    - agent.subagent_dispatch
    - skills.invoke
    - skills.run_script
    - cron.create_job
    - messaging.post_message
    - web.search
    - web.read
    - places.search
  risk_policy:
    read: allow
    draft: allow
    confirm_required: require_confirmation
    write: allow
```

When an operation needs approval, the tool records a pending action instead of mutating the external service. Approve or discard it in the same conversation:

```text
/confirm 0f4c...
/reject 0f4c...
```

Telegram renders these as inline buttons as well.

### Gmail and Google Calendar

The repository includes an interactive OAuth helper. Place a Google Desktop OAuth client secret in the workspace and run:

```bash
scripts/setup-google-oauth.sh \
  --workspace default_workspace \
  --client-secret /path/to/client_secret.json
```

The helper writes token files under `default_workspace/.secrets/google/` and prints the YAML to add to `config.user.yaml`. Add `--with-mutating-email` or `--with-mutating-calendar` only when those operations are required. Review the printed `tools.enabled_capabilities` list: when a global allowlist is present, it must also contain the coordinator, skill, memory, web, task, or place capabilities used by your other agents.

### Todoist

Keep the token outside YAML and name its environment variable in the config:

```yaml
external_tools:
  tasks:
    enabled: true
    provider: todoist
    api_token_env: TODOIST_API_TOKEN
```

Then export it before starting the process:

```bash
export TODOIST_API_TOKEN="..."
uv run build-bot --workspace ./default_workspace server
```

### Web and places

```yaml
websearch:
  provider: brave
  api_key: "..."

webread:
  provider: crawl4ai

places:
  provider: google_places
  api_key: "..."
```

Crawl4AI may require browser dependencies in addition to the Python package. Google Places requests that include rating and review evidence may use billable Places API SKUs; configure quotas and billing limits in Google Cloud.

### Remote tools over MCP

build-bot can act as an MCP (Model Context Protocol) client. It connects to remote servers, discovers their tools, and exposes approved operations through the same capability registry and per-agent policy as built-in tools. If the `mcp` block is absent, no MCP connections are opened.

Add servers under `mcp.servers`:

```yaml
mcp:
  servers:
    movies_db:
      enabled: true
      transport: streamable_http
      url_env: MOVIES_DB_URL
      token_env: MOVIES_DB_TOKEN
      health_path: /health
      required: false
      max_result_chars: 20000
      allowed_tools:
        - movies_search_library
        - movies_recommend
      denied_tools:
        - movies_delete_viewing
```

Only Streamable HTTP is currently supported. Give exactly one of `url` or `url_env`; keep bearer tokens outside YAML by naming their environment variable with `token_env`.

Discovery is fail-closed:

- only exact names from `allowed_tools` become available;
- unlisted tools, newly advertised tools, and unsupported schemas remain quarantined;
- `denied_tools` provides an additional explicit blocklist;
- server descriptions and instructions are treated as untrusted data and cannot grant access or bypass confirmation;
- redirects are refused so authorization headers cannot be replayed to an unconfigured host.

Each admitted tool receives a capability ID such as `mcp.movies_db.movies_recommend`. That ID must also be present in the target agent's `allowed_capabilities` and, when configured, the global `tools.enabled_capabilities` list. In the included workspace, only `movie-assistant` holds the movie capabilities; Pickle delegates movie requests to it.

Inspect connections without exposing URLs, credentials, arguments, or results:

```text
/mcp
/mcp movies_db
```

An unavailable optional server degrades independently; `required: true` instead makes it a startup dependency. Connections recover with bounded backoff, while ambiguous failed tool calls are not automatically retried because a remote mutation may already have happened. Server additions, removals, disabling, and endpoint changes are reconciled while the server is running.

Not yet supported: stdio and OAuth transports, MCP resources, prompts, sampling, and elicitation. See the commented MCP profile in [`default_workspace/config.example.yaml`](default_workspace/config.example.yaml) for timeouts, concurrency, result limits, and per-tool effect policies.

## Daily planner

The daily planner turns calendar, tasks, a weekly routine, and WHOOP readiness into one deterministic plan for the day. A restricted `daily-planner` agent runs from cron each morning, calls `planning_build_day_plan` then `planning_sync_daily_plan`, and reports a short summary. The engine decides what gets scheduled; the agent narrates it and never invents a readiness verdict. The feature is entirely inert until `planning:` is configured — see the commented block in [`default_workspace/config.example.yaml`](default_workspace/config.example.yaml).

`planning.mode` has three values:

- `shadow` (default) — builds and reports the plan but writes nothing to the calendar.
- `review` — writes the plan but leaves it for confirmation before it takes effect.
- `auto` — writes the plan outright.

`planning.planning_calendar_id` must point at a calendar dedicated to the planner's own events. Setting it to `primary` is rejected at config load time — a typo there would otherwise point every planner write at the user's own calendar.

The cron job lives at `default_workspace/crons/daily-plan/CRON.md`, but `default_workspace/crons/` is gitignored like the rest of the mutable workspace state, so this file is **not** version-controlled and is not on a fresh clone. Create it manually on any machine that runs the server; its frontmatter needs `name`, `description`, `agent: daily-planner`, and `schedule: "*/30 5-8 * * *"`.

`planning.plan_deadline` must equal the **last tick** of that cron schedule (`08:30` for `*/30 5-8 * * *`) — the tick that stops waiting for WHOOP and plans regardless. Change the cron schedule and `plan_deadline` together: a deadline later than the last tick means the day is never planned, and one earlier means the deadline tick is not actually the last one.

WHOOP readiness is read through a separate, host-only MCP server (`health_planner` in the commented MCP profile) with `expose_to_agents: false`. The host calls it on the model's behalf; the raw metrics never become an agent-visible capability or reach the model's context — only the classified `green`/`yellow`/`red`/`unknown` verdict does, via `planning_build_day_plan`.

If your workspace sets `tools.enabled_capabilities`, that list is intersected with each agent's own `allowed_capabilities`, not overridden by it. Forgetting the planner's five ids there — `calendar.day_agenda`, `tasks.today`, `tasks.overdue`, `planning.build_day_plan`, `planning.sync_daily_plan` — leaves `daily-planner` with zero tools and no error; the cron session then narrates whatever the model makes of having nothing to call.

## Customize the assistant

### Workspace layout

```text
my-workspace/
├── config.user.yaml          # secrets and user-owned configuration
├── config.runtime.yaml       # generated routing/session state
├── agents/
│   └── <agent-id>/
│       ├── AGENT.md          # model, capabilities, and instructions
│       └── SOUL.md           # optional personality layer
├── skills/
│   └── <skill-id>/
│       ├── SKILL.md          # instructions and resource manifest
│       ├── scripts/          # optional executable helpers
│       └── references/       # optional on-demand context
├── crons/<job-id>/CRON.md    # scheduled prompts
├── memories/                 # durable Markdown memory
├── .history/                 # persisted conversations
├── .event/                   # pending events and confirmations
└── .logs/                    # application and optional LLM traces
```

Relative paths in the configuration are resolved from the workspace root. Keep `config.user.yaml`, `.secrets/`, `.history/`, `.event/`, and `.logs/` out of version control.

### Define an agent

Each agent is a folder containing `AGENT.md`. YAML frontmatter controls runtime behavior; the Markdown body becomes its operating instructions.

```markdown
---
name: Researcher
description: Searches the web and compares sources.
allow_skills: false
max_concurrency: 2
allowed_capabilities:
  - web.search
  - web.read
llm:
  temperature: 0.3
---

You are a research specialist. Compare independent sources, cite claims,
and distinguish strong evidence from uncertain conclusions.
```

Set `default_agent` in `config.user.yaml`, or route a source pattern from a conversation:

```text
/route platform-telegram:123456789:.* pickle
```

### Define a skill

A skill adds reusable domain instructions without adding another always-running service:

```markdown
---
name: release-checklist
description: Prepare and verify a production release.
when_to_use:
  - The user asks to prepare or verify a release
required_tools:
  - bash
---

Follow the project's release checklist, run the declared checks, and report
blocking failures separately from warnings.
```

Validate every skill in a workspace before deployment:

```bash
uv run build-bot --workspace ./default_workspace validate-skills
```

The included `weather-information` skill is a concrete example of a skill with its own constrained script, and `skill-creator` shows how to package new skills.

## Run with Docker

The image runs the long-lived server as an unprivileged user with uid `10001`. Application code and dependencies are built into the image; the workspace is mounted at `/workspace`, keeping configuration, credentials, agents, memory, and history on the host.

### Standalone image

Prepare `default_workspace/config.user.yaml` as described in the quick start. For access to the published WebSocket port, set:

```yaml
api:
  host: 0.0.0.0
  port: 8005
```

Build and run the container:

```bash
docker build -t build-bot:latest .

docker run --name build-bot --restart unless-stopped \
  -v "$PWD/default_workspace:/workspace" \
  -p 127.0.0.1:8005:8005 \
  build-bot:latest
```

On Linux, ensure the mounted workspace is readable and writable by uid `10001`. The port is bound to host loopback intentionally; use an authenticated reverse proxy if remote WebSocket access is required. Telegram does not require an inbound port.

### Docker Compose with `movies_db`

The checked-in [`compose.yaml`](compose.yaml) is the production contract for the included movie assistant. It connects build-bot to a separately deployed `movies_db` MCP service over a private external Docker network. Compose intentionally requires a matching bearer token and will fail before startup when it is absent.

Before the first deployment:

1. Configure `default_workspace/config.user.yaml`, including `api.host: 0.0.0.0` when WebSocket access is needed.
2. Enable the `mcp.servers.movies_db` example from `default_workspace/config.example.yaml`.
3. Make `default_workspace` writable by uid `10001` on Linux.
4. Ensure the separate `movies_db` service is reachable as `movies-db:8765` on the `mcp_internal` network and exposes `/health`.

Create the shared network once, provide the token, and start build-bot:

```bash
docker network inspect mcp_internal >/dev/null 2>&1 || \
  docker network create mcp_internal

export MOVIES_DB_TOKEN="replace-with-the-movies-db-token"
docker compose up -d --build
```

`compose.yaml` supplies `MOVIES_DB_URL=http://movies-db:8765/mcp` inside the container. It publishes only build-bot's WebSocket port on `127.0.0.1`; the MCP port stays private and must not be published by the `movies_db` deployment.

Useful operational commands:

```bash
MOVIES_DB_TOKEN=dummy docker compose config
docker compose logs -f build-bot
docker compose restart build-bot
docker compose down
```

`docker compose down` removes the build-bot container and its default network, but not the bind-mounted workspace or the externally managed `mcp_internal` network. For network layout, health checks, and deployment verification, see [`docs/deployment.md`](docs/deployment.md).

The image is reproducible from the tracked `uv.lock` and does not contain workspace data or secrets. Crawl4AI browser binaries are not installed in the base image; use a derived image if the `webread` tool is required inside Docker.

## Install as a systemd service

The following setup keeps application code in `/opt/build-bot` and mutable workspace data in `/var/lib/build-bot/workspace`. Commands assume `git`, Python 3.11+, and `uv` are already installed on the server.

Create a dedicated user and install the project:

```bash
sudo useradd --system --create-home --home-dir /var/lib/build-bot build-bot
sudo git clone https://github.com/TiniTun/build_bot.git /opt/build-bot
sudo chown -R build-bot:build-bot /opt/build-bot
sudo -u build-bot uv sync --project /opt/build-bot

sudo install -d -o build-bot -g build-bot /var/lib/build-bot/workspace
sudo cp -a /opt/build-bot/default_workspace/. /var/lib/build-bot/workspace/
sudo cp /var/lib/build-bot/workspace/config.example.yaml \
  /var/lib/build-bot/workspace/config.user.yaml
sudo chown -R build-bot:build-bot /var/lib/build-bot/workspace
sudo chmod 600 /var/lib/build-bot/workspace/config.user.yaml
```

Edit `/var/lib/build-bot/workspace/config.user.yaml`, then create `/etc/systemd/system/build-bot.service`:

```ini
[Unit]
Description=build-bot personal AI assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=build-bot
Group=build-bot
WorkingDirectory=/opt/build-bot
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=-/etc/build-bot.env
ExecStart=/opt/build-bot/.venv/bin/build-bot --workspace /var/lib/build-bot/workspace server
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/build-bot

[Install]
WantedBy=multi-user.target
```

Optional provider settings such as `TODOIST_API_TOKEN`, `MOVIES_DB_URL`, and `MOVIES_DB_TOKEN` can be placed in `/etc/build-bot.env` with permissions `0600`. Do not put shell `export` statements in that file; use `NAME=value` lines.

Enable and inspect the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now build-bot
sudo systemctl status build-bot
sudo journalctl -u build-bot -f
```

Update an installation:

```bash
sudo -u build-bot git -C /opt/build-bot pull --ff-only
sudo -u build-bot uv sync --project /opt/build-bot
sudo systemctl restart build-bot
```

The workspace is deliberately outside the Git checkout, so updates do not overwrite agents, skills, memory, credentials, or conversation history.

## Operations and troubleshooting

```bash
# Check the CLI and configuration loading
uv run build-bot --workspace ./default_workspace --help

# Validate skill manifests and referenced files
uv run build-bot --workspace ./default_workspace validate-skills

# Run the long-lived worker stack in the foreground
uv run build-bot --workspace ./default_workspace server
```

- If the process exits during startup, check that `config.user.yaml` exists and all configured paths and YAML values are valid.
- If a provider tool is missing, verify its provider block, global capability allowlist, and the target agent's `allowed_capabilities`.
- If a scheduled job runs but sends nothing, configure `default_delivery_source` and keep the corresponding channel enabled.
- If Telegram ignores a user, compare their numeric ID with `allowed_user_ids`.
- If WebSocket works locally but not remotely, check `api.host`, the firewall, and the authenticated reverse proxy.

## Architecture

- **`core/`** — agents, sessions, history, memory, routing, events, commands, and compaction
- **`server/`** — worker lifecycle, scheduling, delivery, channel bridges, and WebSocket API
- **`channel/`** — platform adapters such as Telegram
- **`provider/`** — LLM, external-service, and MCP adapters
- **`tools/`** — model-callable operations, capability metadata, and confirmation gates
- **`cli/`** — interactive chat and long-running server commands
- **`default_workspace/`** — a usable multi-agent assistant configuration
- **`Dockerfile` / `compose.yaml`** — reproducible container build and production deployment contract

The domain flow is intentionally transport-independent: channels publish typed events, routing selects an agent and session, the agent runs its model/tool loop, and delivery returns the result to the right platform.
