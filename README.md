# build-bot

A personal AI assistant framework. Define agents in Markdown, extend them with skills and tools, and talk to them from the terminal, Telegram, or WebSocket.

## How it works

User input → `EventBus` → `AgentWorker` → `AgentSession` → LLM → response back through the same bus.

Each agent is a folder with an `AGENT.md` file — YAML frontmatter for config, Markdown body for the system prompt.

## Setup

```bash
uv install
cp default_workspace/config.example.yaml default_workspace/config.user.yaml
# fill in your API key and model
```

### Choosing a model

Models are routed through [LiteLLM](https://docs.litellm.ai/docs/providers), so use
the LiteLLM model string. `provider` is just a free-form label.

OpenAI-compatible model:

```yaml
llm:
  provider: openai
  model: gpt-4
  api_key: sk-...
  temperature: 0.7
  max_tokens: 2048
```

Anthropic Opus through LiteLLM:

```yaml
llm:
  provider: anthropic
  model: anthropic/claude-opus-4.8
  api_key: sk-ant-...
  temperature: 0.7
  max_tokens: 4096
```

Provider-specific parameters can be passed through `extra:`, which is forwarded
verbatim to LiteLLM. Per-agent overrides go in the agent's `AGENT.md` frontmatter
under `llm:` and are merged over these defaults.

## Usage

```bash
build-bot chat
build-bot chat --agent my-agent --workspace ./my-workspace
build-bot server   # start the 24/7 event-driven server
```

## Workspace layout

```
my-workspace/
├── config.user.yaml
├── agents/<id>/AGENT.md
└── skills/<id>/SKILL.md
```

## Remote tools over MCP

build-bot can act as an MCP (Model Context Protocol) **client**: it connects to
remote MCP servers, discovers the tools they advertise, and exposes the ones you
approve as ordinary agent tools. There is no separate execution path — an
approved remote tool goes through the same capability registry, policy, and
confirmation machinery as a built-in.

Omit the `mcp:` block entirely and nothing changes: no connections are opened
and every existing tool behaves exactly as before.

### Configuring servers

Add one entry per server under `mcp.servers`. See
`default_workspace/config.example.yaml` for the fully commented version.

```yaml
mcp:
  servers:
    movies_db:
      enabled: true
      transport: streamable_http     # the only transport supported today
      url_env: MOVIES_DB_URL         # or `url:` — exactly one of the two
      token_env: MOVIES_DB_TOKEN     # env var NAME; never the token itself
      health_path: /health           # probed before every MCP initialization
      required: false                # optional: an outage degrades only this server
      max_result_chars: 20000
      allowed_tools:                 # the entire allowlist, matched exactly
        - movies_search_library
```

Adding a second server needs no new top-level configuration — just another key
under `servers`.

### Authentication

Tokens are never written to configuration. `token_env` names an environment
variable, read only at connect time, and attached as
`Authorization: Bearer <token>` to **every** HTTP request the session makes —
the initialization POST, tool listing, tool calls, the resumption/notification
stream, and the terminating DELETE. A token value never appears in logs,
exceptions, `/mcp` output, or anything the model can see. Redirects are refused
so the header can never be replayed to an unconfigured host.

### The allowlist and tool quarantine

Discovery is **fail-closed**. Only the exact names in `allowed_tools` become
capabilities:

- a tool the server advertises but you did not list stays quarantined;
- a tool the server *adds later* stays quarantined until an operator lists it;
- `denied_tools` is a redundant guard for names that must never be exposed;
- a tool whose input schema cannot be represented is quarantined rather than
  published with a guessed schema.

Server-supplied descriptions, annotations, and instructions are untrusted hints.
They are kept for diagnostics but can never grant access, lower a risk level,
enable a retry, or bypass a confirmation gate. A server cannot enable itself.

Server `instructions` stay out of prompts unless you set
`use_server_instructions: true`, and even then they are shown only to an agent
that already holds a capability from that server, clearly delimited as untrusted
guidance, and size-capped.

### Naming

- capability id: `mcp.<server_id>.<remote_tool_name>`
- LLM tool name: `mcp_<server_id>_<remote_tool_name>`, sanitized and truncated to
  provider limits

Names are deterministic and collision-safe: if sanitizing or truncating would
make two tools collide, a short deterministic hash is appended. Two servers
exposing the same remote tool name stay distinct, and a duplicate visible name is
an error rather than a silent overwrite.

### Per-agent access

MCP capabilities are filtered per agent through the existing
`allowed_capabilities` list in `AGENT.md`. In the default workspace only
`movie-assistant` holds movie capabilities; Pickle routes to it and holds none
itself.

A session gets a stable tool schema for its lifetime, but every invocation
re-checks that the server and tool are still globally enabled — disabling a
server takes effect immediately, mid-session.

### Diagnostics

```
/mcp              # all servers: state, tool counts, last error summary
/mcp movies_db    # one server: version, last discovery, enabled/quarantined tools
```

Output never contains credentials, URLs, tool arguments, or raw results.

### Failure behavior

- An optional server that is unreachable degrades on its own; the rest of the bot
  keeps working. A server marked `required: true` fails startup instead.
- Reconnection uses bounded exponential backoff with jitter and repeats the
  `/health` probe before re-initializing.
- A call that fails ambiguously — a timeout in particular — is **never** retried
  automatically, because the server may already have applied it.
- Configuration changes are reconciled live: added, removed, disabled, and
  re-pointed servers are applied without a restart, and a removed or disabled
  server denies new calls before its connection is closed.

### movies_db

The first production profile is `movies_db` over Streamable HTTP. It enables six
read operations plus three explicit, non-destructive writes:
`movies_log_viewing`, `movies_update_viewing`, and `movies_record_feedback`.
Those writes are locally classified as `effect: write`, are never retried, and
run only when the user clearly asks to record or change movie data. Deletion is
not allowlisted, and every unknown future tool stays quarantined.

`MOVIES_DB_URL` is the **MCP endpoint** (`http://movies-db:8765/mcp` in Docker),
not a repository or web URL. In a Docker deployment build-bot reaches it only
over the external `mcp_internal` network, and the MCP port is never published to
the host. See [docs/deployment.md](docs/deployment.md) for network creation,
environment setup, and TLS guidance.

### Not supported yet

stdio and OAuth transports; MCP resources, prompts, sampling, and elicitation;
and a generic confirmed-executor flow for MCP tools configured with
`host_before_call`. The `movies_db` profile therefore permits only explicit
non-destructive writes and keeps deletion unavailable.

## Stack

- **LiteLLM** — talks to any LLM (OpenAI, Anthropic, etc.)
- **Typer + Rich** — CLI
- **FastAPI + uvicorn** — WebSocket API
- **python-telegram-bot** — Telegram channel
- **Pydantic** — config and data validation
- **watchdog** — config hot-reload
- **MCP Python SDK** — Streamable HTTP client for remote tool servers
