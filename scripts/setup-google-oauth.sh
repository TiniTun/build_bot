#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="default_workspace"
CLIENT_SECRET_INPUT=""
RUN_UV_SYNC=1
INCLUDE_MUTATING_EMAIL=0
INCLUDE_MUTATING_CALENDAR=0

usage() {
  cat <<'USAGE'
Usage: scripts/setup-google-oauth.sh [options]

Prepare Google OAuth token files for build_bot Gmail and Google Calendar tools.

Options:
  --workspace DIR              Workspace directory (default: default_workspace)
  --client-secret PATH         Use existing Google OAuth client secret JSON at PATH
  --skip-uv-sync               Do not run "uv sync" before OAuth
  --with-mutating-email        Include Gmail modify scope and print send/delete capabilities
  --with-mutating-calendar     Print calendar update/delete capabilities
  -h, --help                   Show this help

The script writes token files under:
  <workspace>/.secrets/google/gmail_token.json
  <workspace>/.secrets/google/calendar_token.json

It requires an interactive terminal. It prints an OAuth URL instead of opening a
browser, so you can open the link on your laptop and paste the returned code or
redirect URL back into the server terminal.

It does not modify config.user.yaml. Paste the printed YAML block manually.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      WORKSPACE="${2:-}"
      shift 2
      ;;
    --client-secret)
      CLIENT_SECRET_INPUT="${2:-}"
      shift 2
      ;;
    --skip-uv-sync)
      RUN_UV_SYNC=0
      shift
      ;;
    --with-mutating-email)
      INCLUDE_MUTATING_EMAIL=1
      shift
      ;;
    --with-mutating-calendar)
      INCLUDE_MUTATING_CALENDAR=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$WORKSPACE" ]; then
  echo "--workspace must not be empty" >&2
  exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but was not found on PATH." >&2
  exit 1
fi

SECRET_DIR="$WORKSPACE/.secrets/google"
GMAIL_TOKEN="$SECRET_DIR/gmail_token.json"
CALENDAR_TOKEN="$SECRET_DIR/calendar_token.json"

mkdir -p "$SECRET_DIR"
chmod 700 "$WORKSPACE/.secrets" "$SECRET_DIR" 2>/dev/null || true

if [ -n "$CLIENT_SECRET_INPUT" ]; then
  if [ ! -f "$CLIENT_SECRET_INPUT" ]; then
    echo "Client secret file not found: $CLIENT_SECRET_INPUT" >&2
    exit 1
  fi
  client_secret_dir="$(cd "$(dirname "$CLIENT_SECRET_INPUT")" && pwd -P)"
  CLIENT_SECRET="$client_secret_dir/$(basename "$CLIENT_SECRET_INPUT")"
  workspace_abs="$(cd "$WORKSPACE" && pwd -P)"
  case "$CLIENT_SECRET" in
    "$workspace_abs"/*)
      CLIENT_SECRET_CONFIG="${CLIENT_SECRET#"$workspace_abs"/}"
      ;;
    *)
      CLIENT_SECRET_CONFIG="$CLIENT_SECRET"
      ;;
  esac
else
  CLIENT_SECRET="$SECRET_DIR/client_secret.json"
  CLIENT_SECRET_CONFIG=".secrets/google/client_secret.json"
fi

if [ ! -f "$CLIENT_SECRET" ]; then
  cat >&2 <<EOF
Missing Google OAuth client secret:
  $CLIENT_SECRET

Create a Google Cloud OAuth Client ID of type "Desktop app", download its JSON,
then either place it at the path above or rerun:
  scripts/setup-google-oauth.sh --client-secret /path/to/client_secret.json
EOF
  exit 1
fi

if [ ! -t 0 ] || [ ! -t 1 ]; then
  cat >&2 <<'EOF'
This script must run in an interactive terminal.

It prints a Google OAuth URL and waits for you to paste back the authorization
code or redirected localhost URL. Re-run it from a real shell session.
EOF
  exit 1
fi

if [ "$RUN_UV_SYNC" -eq 1 ]; then
  echo "==> Installing/updating project dependencies with uv sync"
  uv sync
fi

echo "==> Generating Google OAuth token files"
GOOGLE_OAUTH_WORKSPACE="$WORKSPACE" \
GOOGLE_OAUTH_MUTATING_EMAIL="$INCLUDE_MUTATING_EMAIL" \
GOOGLE_OAUTH_CLIENT_SECRET="$CLIENT_SECRET" \
uv run python - <<'PY'
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

workspace = Path(os.environ["GOOGLE_OAUTH_WORKSPACE"])
include_mutating_email = os.environ["GOOGLE_OAUTH_MUTATING_EMAIL"] == "1"
base = workspace / ".secrets" / "google"
client_secret = Path(os.environ["GOOGLE_OAUTH_CLIENT_SECRET"])

gmail_scopes = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
if include_mutating_email:
    gmail_scopes.append("https://www.googleapis.com/auth/gmail.modify")

calendar_scopes = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def extract_code(value: str) -> str:
    value = value.strip()
    if not value:
        raise SystemExit("Empty authorization code")
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        code_values = params.get("code")
        if not code_values:
            raise SystemExit("Redirect URL does not contain a code= parameter")
        return code_values[0]
    return value


def read_from_terminal(prompt: str) -> str:
    """Read from the controlling terminal, not Python stdin.

    The Python program is provided through a shell heredoc, so ``input()`` would
    read from the heredoc stream and immediately hit EOF. ``/dev/tty`` keeps the
    OAuth paste prompt connected to the user's actual terminal.
    """
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            print(prompt, end="", flush=True)
            value = tty.readline()
    except OSError as e:
        raise SystemExit(
            "Unable to read from /dev/tty. Re-run from an interactive terminal."
        ) from e
    if value == "":
        raise SystemExit("No authorization code was entered.")
    return value


def issue_token(token_name: str, scopes: list[str]) -> None:
    token_path = base / token_name
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    print()
    print(f"Authorize {token_name}:")
    print(auth_url)
    print()
    print("Open the URL on your laptop. After approval, Google redirects to")
    print("http://localhost:8080/. The page may fail to load; copy the full")
    print("address-bar URL or just the code= value and paste it below.")
    pasted = read_from_terminal("Authorization code or redirected URL: ")
    code = extract_code(pasted)

    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    print(f"Wrote {token_path}")


issue_token("gmail_token.json", gmail_scopes)
issue_token("calendar_token.json", calendar_scopes)
PY

echo
echo "==> Add this block to $WORKSPACE/config.user.yaml"
echo
cat <<EOF
external_tools:
  email:
    enabled: true
    provider: gmail
    credentials_path: "$CLIENT_SECRET_CONFIG"
    token_path: .secrets/google/gmail_token.json
    scopes:
      - https://www.googleapis.com/auth/gmail.readonly
      - https://www.googleapis.com/auth/gmail.compose
EOF

if [ "$INCLUDE_MUTATING_EMAIL" -eq 1 ]; then
  cat <<'EOF'
      - https://www.googleapis.com/auth/gmail.modify
EOF
fi

cat <<EOF
  calendar:
    enabled: true
    provider: google_calendar
    credentials_path: "$CLIENT_SECRET_CONFIG"
    token_path: .secrets/google/calendar_token.json
    calendar_id: primary
    scopes:
      - https://www.googleapis.com/auth/calendar.readonly
      - https://www.googleapis.com/auth/calendar.events

tools:
  enabled_capabilities:
    - email.search
    - email.read
    - email.draft_reply
EOF

if [ "$INCLUDE_MUTATING_EMAIL" -eq 1 ]; then
  cat <<'EOF'
    - email.send
    - email.delete
EOF
fi

cat <<'EOF'
    - calendar.search
    - calendar.availability
    - calendar.create_event
EOF

if [ "$INCLUDE_MUTATING_CALENDAR" -eq 1 ]; then
  cat <<'EOF'
    - calendar.update_event
    - calendar.delete_event
EOF
fi

cat <<'EOF'
  risk_policy:
    read: allow
    draft: allow
    confirm_required: require_confirmation
EOF

echo
echo "Done. Restart build_bot after updating config.user.yaml."
