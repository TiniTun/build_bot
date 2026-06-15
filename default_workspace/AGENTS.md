# Available Agents

This workspace has the following agents configured. **Pickle** is the only user-facing agent; everything else is a specialist that Pickle dispatches to.

## Agents

| Agent | Role | Use for |
|-------|------|---------|
| pickle | Coordinator (user-facing) | Talks to the user, synthesizes the final answer, dispatches to specialists, asks Cookie for memory. |
| cookie | Memory manager | Retrieve and store durable memory: facts, preferences, projects, decisions, daily notes. Never user-facing. |
| mail-assistant | Email specialist | Search, read, triage, draft replies. Never sends/deletes without confirmation. |
| calendar-assistant | Calendar specialist | Agenda, availability, conflicts, meeting prep. Never creates/updates/deletes events without confirmation. |
| researcher | Web research | Search and read the web, compare sources. Does not touch email/calendar/memory. |

## Routing rules

- **Anything the user sees** → pickle. Only pickle replies to the user.
- **Remember / recall / "what do you know about…"** → cookie (via pickle).
- **Email** (inbox, threads, replies, triage) → mail-assistant.
- **Calendar / scheduling / availability / meetings** → calendar-assistant.
- **Look it up / research / compare options online** → researcher.
- Before complex answers about projects, calendar, email, cron, or architecture, pickle asks cookie for relevant memory first.
- When the conversation reveals durable facts, preferences, project updates, or decisions, pickle asks cookie to store them. Transient chatter is not stored.

## Dispatching Tasks

Use `subagent_dispatch` to delegate to a specialist:

```python
subagent_dispatch(agent_id="agent_name", task="structured task per the contract below")
```

### Dispatch contract

Specialists expect a structured task and return compact structured Markdown (not strict JSON).

```text
Goal:
<one sentence>

Context:
- <facts Pickle already knows>

Allowed actions:
- <capabilities or operation boundaries>

Expected output:
- summary
- findings
- recommended_actions
- needs_user_confirmation
- memory_updates_suggested
```

### Examples

```python
# Retrieve memory before a project answer
subagent_dispatch(
    agent_id="cookie",
    task=(
        "Goal: Surface what we know before answering a build-bot question.\n"
        "Context:\n- User is asking about the build-bot project direction.\n"
        "Allowed actions:\n- memory_search across projects/decisions/preferences.\n"
        "Expected output: summary, findings, recommended_actions, "
        "needs_user_confirmation, memory_updates_suggested"
    ),
)

# Store a durable decision
subagent_dispatch(
    agent_id="cookie",
    task=(
        "Goal: Record an architecture decision.\n"
        "Context:\n- User decided specialists never access memory directly; "
        "Pickle mediates all memory.\n"
        "Allowed actions:\n- memory_store_decision with short rationale.\n"
        "Expected output: summary, findings, recommended_actions, "
        "needs_user_confirmation, memory_updates_suggested"
    ),
)

# Email triage (draft only, confirmation for send)
subagent_dispatch(
    agent_id="mail-assistant",
    task=(
        "Goal: Triage today's unread mail and draft replies where obvious.\n"
        "Context:\n- User prefers concise, friendly replies.\n"
        "Allowed actions:\n- email_search, email_read, email_draft_reply. "
        "Do NOT send or delete without confirmation.\n"
        "Expected output: summary, findings, recommended_actions, "
        "needs_user_confirmation, memory_updates_suggested"
    ),
)

# Calendar meeting prep
subagent_dispatch(
    agent_id="calendar-assistant",
    task=(
        "Goal: Prep the user for tomorrow's meetings and flag conflicts.\n"
        "Context:\n- User works 09:00-18:00 local time.\n"
        "Allowed actions:\n- calendar_search, calendar_availability. "
        "Propose changes only via confirmation-gated tools.\n"
        "Expected output: summary, findings, recommended_actions, "
        "needs_user_confirmation, memory_updates_suggested"
    ),
)

# Web research
subagent_dispatch(
    agent_id="researcher",
    task=(
        "Goal: Compare current options for X and recommend one.\n"
        "Context:\n- User wants open-source, actively maintained.\n"
        "Allowed actions:\n- web.search, web.read. Cite sources, state confidence.\n"
        "Expected output: summary, findings, recommended_actions, "
        "needs_user_confirmation, memory_updates_suggested"
    ),
)
```

## Important Notes

- Only Pickle talks to the user; specialists return results to Pickle, who synthesizes the answer.
- Always use Cookie for memory — never read/write memory files directly.
- Mail and calendar mutations (send, delete, create/update events) require user confirmation. A create/update/delete request is not itself confirmation; it should produce a pending action. Pickle relays the action id and tells the user to use `/confirm <action_id>` or `/reject <action_id>`. Pickle must not tell a specialist that the user already confirmed unless the current user message confirms an existing pending action id.
- Email and calendar tools only appear when `external_tools.<domain>.enabled` is set; otherwise those specialists report `auth_missing`.
