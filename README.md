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

## Stack

- **LiteLLM** — talks to any LLM (OpenAI, Anthropic, etc.)
- **Typer + Rich** — CLI
- **FastAPI + uvicorn** — WebSocket API
- **python-telegram-bot** — Telegram channel
- **Pydantic** — config and data validation
- **watchdog** — config hot-reload
