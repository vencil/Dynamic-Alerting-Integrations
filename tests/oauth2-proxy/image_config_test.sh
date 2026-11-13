#!/usr/bin/env bash
# ADR-027 §9 item 9: run `oauth2-proxy --config-test` in the image the
# tenant-api chart deploys (by digest), with the args and config file the
# chart renders, for every variant TestChartConfig wrote.
#
#   OAUTH2_PROXY_RENDER_OUT=DIR go test ./...   # in tests/oauth2-proxy
#   bash tests/oauth2-proxy/image_config_test.sh DIR
#
# Each DIR/<variant>/ holds oauth2-proxy.yaml, args.json and image. The config
# is mounted at the path --alpha-config names, a placeholder client secret at
# the path the config's clientSecretFile names; nothing contacts an IdP.
#
# ADR-027 §9 item 3 (the 401): a variant whose provider needs no IdP discovery
# at start (github) is then STARTED, the same image, args and files, and asked
# without a session: GET /api/v1/me must answer 401 (--api-route), and GET /
# must not reach an upstream (no session: the sign-in page or a redirect to
# it). Providers that discover their IdP at start (oidc, gitlab) are only
# config-tested; TestChartAPIRouteIsTheSameInEveryVariant holds their
# --api-route to the started one's.
# Needs docker, curl and python3 with PyYAML. Exit 0 all valid, 1 any invalid,
# 2 usage or no variant.
# adr-check: ADR-027/9.9a
set -euo pipefail

dir="${1:-}"
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $0 RENDER_OUT_DIR" >&2
  exit 2
fi

tmp="$(mktemp -d)"
probe_cids=""
trap 'for c in $probe_cids; do docker rm -f "$c" >/dev/null 2>&1 || true; done; rm -rf "$tmp"' EXIT
printf 'placeholder-client-secret' > "$tmp/client-secret"
chmod 0644 "$tmp/client-secret"

# The library these files were loaded with (go.mod) must be the version the
# digest actually runs: a tag bump that forgets the digest would otherwise
# pass here while the cluster keeps running the old release.
gomod="$(dirname "$0")/go.mod"
want="$(sed -n 's#^[[:space:]]*\(require[[:space:]]\+\)\{0,1\}github.com/oauth2-proxy/oauth2-proxy/v7 \(v[^[:space:]]*\)$#\2#p' "$gomod" | head -n 1)"
if [ -z "$want" ]; then
  echo "::error::$gomod: no direct require of github.com/oauth2-proxy/oauth2-proxy/v7" >&2
  exit 2
fi
checked_images=""

# probe_unauthenticated IMAGE CFG_HOST CFG_PATH SECRET_PATH ARGS...: start the
# proxy, then the two requests above. 1 on any wrong answer.
# adr-check: ADR-027/9.3b
probe_unauthenticated() {
  local image="$1" cfg_host="$2" cfg_path="$3" secret_path="$4"
  shift 4
  local cid port code_api code_root i rc=0
  # No --rm: a container that exits at once must keep its logs for the
  # error below; it is removed at the end (and by the EXIT trap).
  cid="$(docker run -d -p 127.0.0.1::4180 \
    -e OAUTH2_PROXY_COOKIE_SECRET=0123456789abcdef0123456789abcdef \
    -v "$cfg_host:$cfg_path:ro" \
    -v "$tmp/client-secret:$secret_path:ro" \
    "$image" "$@")" || { echo "::error::$image: docker run failed" >&2; return 1; }
  probe_cids="$probe_cids $cid"
  port="$(docker port "$cid" 4180/tcp | head -n 1 | sed 's/.*://')"
  if [ -z "$port" ]; then
    echo "::error::$image: no published port for 4180 (container exited?)" >&2
    docker logs "$cid" >&2 || true
    docker rm -f "$cid" >/dev/null 2>&1 || true
    return 1
  fi
  # Every request is bounded (--max-time) and readiness has one overall
  # deadline, so a container that accepts a connection but never answers
  # fails this check instead of hanging it.
  deadline=$((SECONDS + 30))
  while :; do
    if curl -fsS --connect-timeout 2 --max-time 3 -o /dev/null "http://127.0.0.1:$port/ping" 2>/dev/null; then break; fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "::error::$image: no /ping answer within 30s" >&2
      docker logs "$cid" >&2 || true
      docker rm -f "$cid" >/dev/null 2>&1 || true
      return 1
    fi
    sleep 1
  done
  # A timeout leaves curl's 000 in the code, which fails the checks below.
  code_api="$(curl -sS --connect-timeout 2 --max-time 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/v1/me")" || true
  code_root="$(curl -sS --connect-timeout 2 --max-time 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/")" || true
  docker rm -f "$cid" >/dev/null 2>&1 || true
  echo "   unauthenticated: GET /api/v1/me -> $code_api, GET / -> $code_root"
  if [ "$code_api" != "401" ]; then
    echo "::error::GET /api/v1/me without a session answered $code_api, want 401 (ADR-027 §5, §9 item 3)" >&2
    rc=1
  fi
  # 403 = oauth2-proxy's sign-in page, 302 = a redirect to sign in; anything
  # else (200 from the portal upstream, 502 trying to reach it) let the
  # request through without a session.
  case "$code_root" in
    302|403) ;;
    *) echo "::error::GET / without a session answered $code_root, want the sign-in page (403) or a redirect to it (302)" >&2; rc=1 ;;
  esac
  return "$rc"
}

n=0
fail=0
started=0
for v in "$dir"/*/; do
  v="${v%/}"
  [ -f "$v/args.json" ] || continue
  n=$((n + 1))
  image="$(tr -d '\n' < "$v/image")"
  case "$image" in
    *@sha256:*) ;;
    *) echo "::error::$v: image $image is not pinned by digest" >&2; fail=1; continue ;;
  esac
  case " $checked_images " in
    *" $image "*) ;;
    *)
      got="$(docker run --rm "$image" --version 2>&1 | sed -n 's/^oauth2-proxy \(v[^[:space:]]*\).*/\1/p' | head -n 1)"
      if [ "$got" != "$want" ]; then
        echo "::error::$image reports ${got:-no version}; tests/oauth2-proxy/go.mod loads $want" >&2
        fail=1
      fi
      checked_images="$checked_images $image"
      ;;
  esac
  mapfile -t args < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1], encoding="utf-8"))))' "$v/args.json")
  cfg_path=""
  for a in "${args[@]}"; do
    case "$a" in --alpha-config=*) cfg_path="${a#--alpha-config=}" ;; esac
  done
  secret_path="$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["providers"][0]["clientSecretFile"])' "$v/oauth2-proxy.yaml")"
  if [ -z "$cfg_path" ] || [ -z "$secret_path" ]; then
    echo "::error::$v: no --alpha-config arg or no clientSecretFile" >&2
    fail=1
    continue
  fi
  echo "== $(basename "$v"): $image"
  rc=0
  docker run --rm \
    -e OAUTH2_PROXY_COOKIE_SECRET=0123456789abcdef0123456789abcdef \
    -v "$(cd "$v" && pwd)/oauth2-proxy.yaml:$cfg_path:ro" \
    -v "$tmp/client-secret:$secret_path:ro" \
    "$image" "${args[@]}" --config-test || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "::error::$(basename "$v"): oauth2-proxy --config-test exited $rc" >&2
    fail=1
    continue
  fi
  provider="$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["providers"][0]["provider"])' "$v/oauth2-proxy.yaml")"
  if [ "$provider" = "github" ]; then
    started=$((started + 1))
    probe_unauthenticated "$image" "$(cd "$v" && pwd)/oauth2-proxy.yaml" "$cfg_path" "$secret_path" "${args[@]}" || fail=1
  fi
done

if [ "$n" -eq 0 ]; then
  echo "::error::no variant under $dir (did TestChartConfig run with OAUTH2_PROXY_RENDER_OUT?)" >&2
  exit 2
fi
if [ "$started" -eq 0 ]; then
  echo "::error::no github variant was started: the unauthenticated 401 (ADR-027 §9 item 3) went unchecked" >&2
  fail=1
fi
echo "checked $n variant(s), started $started"
exit "$fail"
