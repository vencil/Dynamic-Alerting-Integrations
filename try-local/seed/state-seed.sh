#!/bin/sh
# try-local seed (state): copy the demo tenant-api state records (a saved
# view) into the state volume, then start tenant-api (#2885).
#
# Runs as tenant-api's own entrypoint, so the copies are owned by the same
# non-root user tenant-api writes them as. A record already in the volume is
# never overwritten: a view the user changed survives `docker compose up`.
# The demo records live here, not in conf.d, because tenant-api no longer
# reads its state from conf.d (#2856) and warns about any it finds there.
set -eu

SRC="${SEED_STATE_DIR:-/seed/state}"
DST="${TA_STATE_DIR:?TA_STATE_DIR must be set}"

for f in "$SRC"/*.yaml; do
  [ -e "$f" ] || continue
  t="$DST/${f##*/}"
  if [ -e "$t" ]; then
    echo "[seed-state] ${f##*/} already in the state store (kept)"
  else
    cp "$f" "$t"
    echo "[seed-state] seeded ${f##*/}"
  fi
done

exec "${TENANT_API_BIN:-/usr/local/bin/tenant-api}" "$@"
