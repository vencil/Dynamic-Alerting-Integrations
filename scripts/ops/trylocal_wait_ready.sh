#!/usr/bin/env bash
# trylocal_wait_ready.sh — wait for the try-local compose stack to come up, and
# FAIL when a service is down instead of passing over it (#2909).
#
# Usage (from the compose project directory, i.e. try-local/):
#   bash ../scripts/ops/trylocal_wait_ready.sh [TIMEOUT_SECONDS]   # default 300
#
# Exit: 0 = every service ready, 1 = a service failed or the timeout passed.
#
# Why this replaced the workflow's inline wait (#2909): the old predicate was
#   docker compose ps --format json | jq 'select(.Health != "" and .Health != "healthy")'
# and it had three blind spots, all of which pass a service that is not running:
#   1. it only looked at .Health — a service with no healthcheck was never
#      checked at all, and neither was .State;
#   2. `docker compose ps` without --all hides exited containers, so a service
#      that died simply vanished from the list it was filtering;
#   3. a service whose container was never created is not listed either.
# The recipe-preview image could not import app.py for 6 weeks (#2873) while
# this step reported "all healthchecked services healthy" every night.
#
# What counts as ready, per service in `docker compose config`:
#   - one-shot (restart: "no", derived from the compose file — not a hand list):
#     exited with code 0. Still running = keep waiting. Non-zero = fail now.
#   - every other service: a container exists, State.Status == running,
#     RestartCount == 0, and — if it has a healthcheck — health == healthy.
#     A restart or a non-running state (exited, dead, restarting…) fails now:
#     a crash-looping container is "running" for a moment between restarts, so
#     State alone can be polled at a lucky instant; RestartCount cannot.
#     `created` is the only non-running state that is still worth waiting on.
#
# The data comes from `docker inspect` (one stable JSON shape) rather than
# `docker compose ps --format json`, whose output changed from a JSON array to
# one object per line between compose releases.
set -euo pipefail

TIMEOUT="${1:-300}"
INTERVAL="${TRYLOCAL_WAIT_INTERVAL:-5}"

command -v jq >/dev/null 2>&1 || { echo "::error::jq not found (required)" >&2; exit 1; }

config="$(docker compose config --format json)"
services="$(jq -c '.services | keys' <<<"$config")"
oneshots="$(jq -c '[.services | to_entries[] | select(.value.restart == "no") | .key]' <<<"$config")"
if [ "$(jq 'length' <<<"$services")" -eq 0 ]; then
  echo "::error::docker compose config listed no services" >&2
  exit 1
fi
echo "services: $(jq -r 'join(" ")' <<<"$services")"
echo "one-shot (restart: \"no\"): $(jq -r 'join(" ")' <<<"$oneshots")"

# One line per service: "<ok|wait|fail> <service> <reason>".
# shellcheck disable=SC2016  # $vars below are jq variables, not shell.
classify='
  (map({key: .Config.Labels["com.docker.compose.service"], value: .}) | from_entries) as $by
  | $services[] as $s
  | $by[$s] as $c
  | if $c == null then "wait \($s) no container yet"
    elif ($oneshots | any(. == $s)) then
      if $c.State.Status == "exited" then
        (if $c.State.ExitCode == 0 then "ok \($s) one-shot exited 0"
         else "fail \($s) one-shot exited \($c.State.ExitCode)" end)
      else "wait \($s) one-shot \($c.State.Status)" end
    elif ($c.RestartCount // 0) > 0 then
      "fail \($s) restarted \($c.RestartCount) time(s), state \($c.State.Status)"
    elif $c.State.Status == "created" then "wait \($s) created, not started"
    elif $c.State.Status != "running" then "fail \($s) state \($c.State.Status)"
    elif (($c.State.Health.Status // "") | . != "" and . != "healthy") then
      "wait \($s) health \($c.State.Health.Status)"
    else "ok \($s) running\(if $c.State.Health then ", healthy" else "" end)" end
'

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  ids="$(docker compose ps --all --quiet)"
  if [ -n "$ids" ]; then
    # A container removed between `ps` and `inspect` makes inspect fail;
    # that is a poll to retry, not a verdict (the deadline still applies).
    # shellcheck disable=SC2086  # word-split the id list on purpose
    inspect="$(docker inspect $ids 2>/dev/null)" || inspect='[]'
  else
    inspect='[]'
  fi
  report="$(jq -r --argjson services "$services" --argjson oneshots "$oneshots" \
    "$classify" <<<"$inspect")"

  if grep -q '^fail ' <<<"$report"; then
    echo "$report"
    grep '^fail ' <<<"$report" | while read -r _ svc reason; do
      echo "::error::try-local service ${svc} is not running: ${reason}"
    done
    exit 1
  fi
  if ! grep -q '^wait ' <<<"$report"; then
    echo "$report"
    echo "all try-local services ready"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "$report"
    echo "::error::try-local services not ready within ${TIMEOUT}s: $(grep '^wait ' <<<"$report" | awk '{print $2}' | xargs)"
    exit 1
  fi
  sleep "$INTERVAL"
done
