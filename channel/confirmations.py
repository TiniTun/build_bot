"""Turn pending-action confirmation prompts into Telegram inline buttons.

Pure helpers with no Telegram dependency so they stay unit-testable. The agent
may emit outbound text that instructs the user to send ``/confirm <id>`` /
``/reject <id>``; on Telegram that text is replaced with inline buttons whose
clicks are routed back through the normal command flow as the same slash
commands. No calendar/mail/provider logic lives here — only text shaping.
"""

import re
from dataclasses import dataclass

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_COMMAND_RE = re.compile(rf"/(confirm|reject)\s+({_UUID})")
_CALLBACK_RE = re.compile(rf"^(confirm|reject):({_UUID})$")


@dataclass(frozen=True)
class ConfirmationPrompt:
    """Sanitized confirmation text plus the pending action ids for the buttons."""

    text: str
    confirm_id: str
    reject_id: str


def parse_confirmation(content: str) -> tuple[str, ConfirmationPrompt | None]:
    """Split confirmation instructions out of ``content``.

    Returns ``(sanitized_text, prompt)``. ``prompt`` is ``None`` when no
    ``/confirm <uuid>`` or ``/reject <uuid>`` command is present, in which case
    the text is returned unchanged. When commands are found, the raw command
    lines and their colon-terminated lead-in lines (e.g. "..., отправьте:") are
    removed from the visible text.
    """
    confirm_id: str | None = None
    reject_id: str | None = None
    for match in _COMMAND_RE.finditer(content):
        kind, action_id = match.group(1), match.group(2)
        if kind == "confirm" and confirm_id is None:
            confirm_id = action_id
        elif kind == "reject" and reject_id is None:
            reject_id = action_id

    if confirm_id is None and reject_id is None:
        return content, None

    # Mirror the available id so both buttons target the same pending action
    # even if the agent only printed one of the two commands.
    confirm_id = confirm_id or reject_id
    reject_id = reject_id or confirm_id
    assert confirm_id is not None and reject_id is not None

    keep: list[str] = []
    for line in content.splitlines():
        if _COMMAND_RE.search(line):
            # Drop the command line and any colon lead-in directly above it.
            if keep and keep[-1].rstrip().endswith(":"):
                keep.pop()
            continue
        keep.append(line)

    text = "\n".join(keep).strip()
    return text, ConfirmationPrompt(
        text=text, confirm_id=confirm_id, reject_id=reject_id
    )


def callback_to_command(data: str | None) -> str | None:
    """Convert button callback data into the equivalent slash command.

    ``confirm:<uuid>`` -> ``/confirm <uuid>`` and ``reject:<uuid>`` ->
    ``/reject <uuid>``. Returns ``None`` for anything that does not match, so
    unrelated callbacks are ignored rather than mis-routed.
    """
    match = _CALLBACK_RE.match(data or "")
    if not match:
        return None
    return f"/{match.group(1)} {match.group(2)}"
