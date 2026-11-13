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
# Needs docker and python3 with PyYAML. Exit 0 all valid, 1 any invalid,
# 2 usage or no variant.
# adr-check: ADR-027/9.9a
set -euo pipefail

dir="${1:-}"
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $0 RENDER_OUT_DIR" >&2
  exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
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

n=0
fail=0
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
  fi
done

if [ "$n" -eq 0 ]; then
  echo "::error::no variant under $dir (did TestChartConfig run with OAUTH2_PROXY_RENDER_OUT?)" >&2
  exit 2
fi
echo "checked $n variant(s)"
exit "$fail"
