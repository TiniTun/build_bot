# build-bot

## Status
active

## Context
- build-bot is a personal AI assistant framework.
- Agents are defined in Markdown folders using `AGENT.md` files with YAML frontmatter for configuration and Markdown body for system prompts.
- The framework can be used from the terminal, Telegram, or via WebSocket.
- Core flow described in the README: user input → `EventBus` → `AgentWorker` → `AgentSession` → LLM → response back through the same bus.
- Typical workspace layout includes `agents/<id>/AGENT.md`, and `skills/<id>/SKILL.md`.
- Usage commands include `build-bot chat`, `build-bot chat --agent my-agent --workspace ./my-workspace`, and `build-bot server` for a 24/7 event-driven server.
- Stack from README: LiteLLM, Typer + Rich, FastAPI + uvicorn, python-telegram-bot, Pydantic, and watchdog.

## 2026-06-14T13:25:19Z

User preference for build_bot: prefer to write a short technical specification first, then write code.

