#!/usr/bin/env bash
# forge_e2e_run.sh — tenant-api real-forge E2E against a self-hosted GitLab CE
# (issue #616, Track 2). Spins up a PINNED GitLab CE container, waits for real
# readiness (not just /-/health — also rides out Gitaly/Sidekiq warmup via
# retried seeding), mints api + read_api PATs, then runs the forge_e2e-tagged
# Go tests against it. Used both locally (Windows Git Bash / Linux) and by the
# nightly CI workflow.
#
# Usage:
#   bash scripts/ops/forge_e2e_run.sh            # reuse-or-start CE, run, keep container
#   TEARDOWN=1 bash scripts/ops/forge_e2e_run.sh # remove the container afterward
#
# Knobs (env): GITLAB_CE_IMAGE, GITLAB_PORT, CONTAINER, GO_TEST_TIMEOUT.
set -euo pipefail

GITLAB_CE_IMAGE="${GITLAB_CE_IMAGE:-gitlab/gitlab-ce:18.11.3-ce.0}"   # PINNED — never :latest (#616 DoD)
GITLAB_PORT="${GITLAB_PORT:-8929}"
CONTAINER="${CONTAINER:-vibe-gitlab-ce-e2e}"
GO_TEST_TIMEOUT="${GO_TEST_TIMEOUT:-30m}"
BASE_URL="http://localhost:${GITLAB_PORT}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TENANT_API_DIR="${here}/components/tenant-api"

log() { printf '\n=== %s ===\n' "$*"; }

ensure_container() {
  if [ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)" = "true" ]; then
    log "reusing running ${CONTAINER}"
    return
  fi
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  log "starting ${GITLAB_CE_IMAGE} (no root password → auto-generated; trimmed services)"
  # NOTE: do NOT set GITLAB_ROOT_PASSWORD — GitLab 18 rejects "common" strings;
  # the auto-generated password is unused (tokens are minted via rails runner).
  docker run -d --name "${CONTAINER}" --shm-size=256m -p "${GITLAB_PORT}:${GITLAB_PORT}" \
    -e GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:${GITLAB_PORT}'; prometheus_monitoring['enable']=false; registry['enable']=false; gitlab_kas['enable']=false; puma['worker_processes']=2; sidekiq['max_concurrency']=8" \
    "${GITLAB_CE_IMAGE}" >/dev/null
}

wait_health() {
  # NB: GitLab's /-/health is IP-whitelisted to 127.0.0.1 (monitoring_whitelist),
  # so from outside the container it returns 404 even when ready. Use the
  # container's own HEALTHCHECK status (authoritative); fall back to an
  # in-container /-/health probe if the image defines no healthcheck.
  log "waiting for ${CONTAINER} to report healthy (container HEALTHCHECK)"
  for i in $(seq 1 120); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "${CONTAINER}" 2>/dev/null || true)"
    if [ "${status}" = "healthy" ]; then
      echo "  container healthy after ~$((i * 6))s"
      return 0
    fi
    # Fail fast: if the container exited during boot (e.g. the intermittent
    # omnibus logrotate reconfigure flake), don't burn the full timeout —
    # return non-zero so main() can recreate + retry.
    if [ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)" != "true" ]; then
      echo "  ${CONTAINER} exited during boot (reconfigure flake?)" >&2
      return 1
    fi
    if [ -z "${status}" ] && \
      [ "$(docker exec "${CONTAINER}" curl -s -o /dev/null -w '%{http_code}' "http://localhost:${GITLAB_PORT}/-/health" 2>/dev/null || true)" = "200" ]; then
      echo "  in-container /-/health 200 after ~$((i * 6))s"
      return 0
    fi
    sleep 6
  done
  echo "  timed out waiting for ${CONTAINER} to become healthy" >&2
  return 1
}

# mint_token <scope> <name> — create a PAT for root via rails runner and echo
# the plaintext. Retried: even after /-/health=200, the Rails/DB layer can
# still be warming up (phantom readiness → transient failures, #616 round-2).
mint_token() {
  local scope="$1" name="$2" out=""
  for attempt in 1 2 3 4 5; do
    if out="$(docker exec "${CONTAINER}" gitlab-rails runner \
      "puts User.find_by_username('root').personal_access_tokens.create!(scopes:['${scope}'], name:'${name}', expires_at: 1.day.from_now).token" 2>/dev/null \
      | tr -d '\r' | grep -E '^glpat-' | head -1)"; then
      if [ -n "${out}" ]; then echo "${out}"; return; fi
    fi
    sleep 10
  done
  echo "FATAL: could not mint ${scope} token after retries" >&2
  dump_logs_and_die
}

dump_logs_and_die() {
  log "dumping ${CONTAINER}:/var/log/gitlab to ./gitlab-ce-logs.tar.gz (failure artifact)"
  docker exec "${CONTAINER}" tar czf /tmp/gitlab-logs.tgz -C /var/log gitlab 2>/dev/null || true
  docker cp "${CONTAINER}:/tmp/gitlab-logs.tgz" "${here}/gitlab-ce-logs.tar.gz" 2>/dev/null || true
  exit 1
}

main() {
  # Boot with one retry: a running container is reused; otherwise we fresh-boot,
  # and if the boot flakes (omnibus reconfigure), recreate once before giving up.
  local booted=0
  for attempt in 1 2; do
    ensure_container
    if wait_health; then booted=1; break; fi
    log "boot attempt ${attempt} did not become healthy — recreating"
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  done
  if [ "${booted}" != "1" ]; then
    echo "FATAL: GitLab CE did not become healthy after retries" >&2
    dump_logs_and_die
  fi

  log "minting api + read_api PATs (root, via rails runner)"
  local api_token ro_token
  api_token="$(mint_token api "e2e-api-$(date +%s)")"
  ro_token="$(mint_token read_api "e2e-ro-$(date +%s)")"
  echo "  api token: ${api_token:0:12}…  ro token: ${ro_token:0:12}…"

  log "running forge_e2e (GitLab) Go tests"
  set +e
  ( cd "${TENANT_API_DIR}" && \
    E2E_GITLAB_API_URL="${BASE_URL}" \
    E2E_GITLAB_TOKEN="${api_token}" \
    E2E_GITLAB_RO_TOKEN="${ro_token}" \
    go test -tags forge_e2e -timeout "${GO_TEST_TIMEOUT}" -v \
      ${GO_TEST_RUN:+-run "${GO_TEST_RUN}"} ./test/forgee2e/... )
  local rc=$?
  set -e
  if [ "${rc}" -ne 0 ]; then dump_logs_and_die; fi

  if [ "${TEARDOWN:-0}" = "1" ]; then
    log "TEARDOWN=1 → removing ${CONTAINER}"
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  fi
  log "forge E2E (GitLab CE) PASSED"
}

main "$@"
