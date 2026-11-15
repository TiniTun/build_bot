#!/bin/sh
# Retries a network-touching command: the Pi's resolver drops lookups under load.
# Usage: ci/retry.sh <command...>   (RETRY_ATTEMPTS, RETRY_DELAY override the defaults)
set -u
attempts=${RETRY_ATTEMPTS:-3}
delay=${RETRY_DELAY:-15}
n=1
while true; do
  "$@" && exit 0
  status=$?
  if [ "$n" -ge "$attempts" ]; then
    echo "giving up after $n attempts: $*" >&2
    exit "$status"
  fi
  echo "attempt $n/$attempts failed (exit $status): $*; retrying in ${delay}s" >&2
  n=$((n + 1))
  sleep "$delay"
done
