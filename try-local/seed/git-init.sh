#!/bin/sh
# try-local seed (1/2): git-init the bind-mounted config repo.
#
# tenant-api runs TA_WRITE_MODE=direct (commit-on-write), so a portal "Save"
# needs a real git repo to commit into. This one-shot creates it (idempotent),
# so even Mode 0 (`docker compose up da-portal tenant-api`) gets the
# Save-lands-a-real-commit wow. It has NO dependency on the monitoring stack,
# which keeps Mode 0 to just the core twins (+ this fast git init).
set -eu

CONF_DIR="${CONF_DIR:-/conf.d}"

# git refuses to operate in a bind-mounted dir owned by another uid without
# this ("detected dubious ownership").
git config --global --add safe.directory '*' 2>/dev/null || true

cd "$CONF_DIR"
if [ ! -d .git ]; then
  git init -q
  git config user.email "seed@local"
  git config user.name "try-local seed"
  git add -A
  git commit -q -m "try-local seed: initial tenant config" || true
  echo "[git-init] initialized config repo at ${CONF_DIR}"
else
  echo "[git-init] repo already present at ${CONF_DIR} (idempotent skip)"
fi

# This seed runs as root, but tenant-api (commit-on-write) runs as a non-root
# user and shares this .git via the bind mount. Make .git group/world-writable
# so tenant-api can create .git/index.lock and commit. Local-dev only.
chmod -R a+rwX "$CONF_DIR/.git" 2>/dev/null || true
