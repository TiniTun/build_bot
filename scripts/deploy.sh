#!/bin/bash
# Runs on the Pi from the commit the pipeline just checked out (or by hand for a rollback:
# `git checkout <sha> && ./scripts/deploy.sh`). Syncs the venv from uv.lock, restarts the
# systemd unit, then fails unless the unit is still active, on the same process, after a settle
# period. Never touches untracked files: default_workspace config, history and secrets stay put.
set -euo pipefail

cd "$(dirname "$0")/.."
# A non-login ssh shell does not read the profile that puts uv on PATH.
export PATH="$HOME/.local/bin:$PATH"

UNIT="${BUILD_BOT_UNIT:-build-bot.service}"
SETTLE_SECONDS="${DEPLOY_SETTLE_SECONDS:-15}"
SHA="$(git rev-parse --short HEAD)"
echo "deploying $SHA to $UNIT"

./ci/retry.sh uv sync --frozen

sudo systemctl restart "$UNIT"

prop() { systemctl show "$UNIT" -p "$1" --value; }
unit_state() { echo "$(prop ActiveState) $(prop MainPID) $(prop NRestarts)"; }

# Baseline right after the restart: a crash-and-restart during the settle period changes the
# main PID and NRestarts; a crash without Restart= leaves ActiveState other than active.
baseline="$(unit_state)"
sleep "$SETTLE_SECONDS"
current="$(unit_state)"

read -r active _ _ <<< "$current"
if [ "$active" != "active" ] || [ "$current" != "$baseline" ]; then
  echo "$UNIT is not running cleanly (ActiveState MainPID NRestarts: before '$baseline', after '$current')" >&2
  journalctl -u "$UNIT" -n 50 --no-pager >&2 || true
  exit 1
fi

echo "deployed $SHA"
