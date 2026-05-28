# build-bot

## Status
active

## Context
- build-bot is a personal AI assistant framework.
- Agents are defined in Markdown folders using `AGENT.md` files with YAML frontmatter for configuration and Markdown body for system prompts.
- The framework can be used from the terminal, Telegram, or via WebSocket.
- Core flow described in the README: user input → `EventBus` → `AgentWorker` → `AgentSession` → LLM → response back through the same bus.
- Typical workspace layout includes `config.user.yaml`, `agents/<id>/AGENT.md`, and `skills/<id>/SKILL.md`.
- Setup uses `uv install` and a copied `default_workspace/config.user.yaml.example` to `default_workspace/config.user.yaml` for API key/model configuration.
- Usage commands include `build-bot chat`, `build-bot chat --agent my-agent --workspace ./my-workspace`, and `build-bot server` for a 24/7 event-driven server.
- Stack from README: LiteLLM, Typer + Rich, FastAPI + uvicorn, python-telegram-bot, Pydantic, and watchdog.

## Progress
- Read the top-level project README and extracted foundational architecture, setup, usage, and stack details.

## Next Steps
- [ ] Update this memory when more project-specific implementation details or decisions are learned.

## Blockers
- README only provides high-level framework context; no deeper implementation or roadmap details yet.
