#!/usr/bin/env python3
"""federation_keygen.py — federation JWT 簽章金鑰的生成 / 輪替工具。

ADR-020 IV-2l。產生 tenant-api 簽 federation token 用的 RS256 keypair,
並輸出兩個下游消費端要的 artifact:

  - 私鑰  → 直接吐成 Kubernetes Secret manifest 到 stdout。私鑰**不落地、
            不進剪貼簿**:`da-tools fed-key | kubectl apply -f -`,記憶體
            →pipe→etcd。
  - 公鑰  → 寫成 JWKS 檔(--jwks-out)。公鑰非機密,可正常處理 —— 把它
            填進 federation-gateway chart 的 jwt.jwks value。

每把公鑰的 `kid` 是它的 RFC 7638 JWK thumbprint(公鑰的確定性指紋)。
tenant-api 簽 token 時對自己載入的金鑰算同一個 thumbprint 當 `kid`
header,故兩邊天然一致 —— gateway 的 jwt_authn 可用 `kid` O(1) 選鑰、
不必遍歷 JWKS 試每一把(輪替期的 RSA-CPU 放大攻擊面因此關閉)。

用法:
  # 首次 bootstrap:產一把新金鑰
  da-tools fed-key | kubectl apply -f -
  #   → Secret manifest 進 stdout;JWKS 寫到 ./federation-jwks.json

  # 輪替:產新金鑰,並把新公鑰併進現有 JWKS(舊+新並存,kid 區分)
  da-tools fed-key --rotate --existing-jwks federation-jwks.json | kubectl apply -f -
  #   → 新私鑰 Secret 進 stdout;合併後的 JWKS 覆寫 --jwks-out
  #   輪替順序見 docs/internal/federation-key-rotation-runbook.md
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402


def _b64u(raw: bytes) -> str:
    """base64url, no padding (the JOSE encoding for JWK members)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _rfc7638_kid(n_b64u: str, e_b64u: str) -> str:
    """RFC 7638 JWK thumbprint of an RSA public key, used as the `kid`.

    SHA-256 of the canonical JWK — the required RSA members {e, kty, n},
    lexicographically ordered, no whitespace — then base64url. tenant-api
    computes the identical value in Go, so the kid matches by construction.
    """
    canonical = json.dumps(
        {"e": e_b64u, "kty": "RSA", "n": n_b64u},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64u(hashlib.sha256(canonical.encode("utf-8")).digest())


def _generate_keypair(bits: int):
    """Generate an RSA keypair via openssl. Returns (private_pem, jwk).

    openssl is shelled out to deliberately — it keeps this tool free of a
    third-party crypto dependency, and `openssl genrsa` always uses the
    public exponent 65537 (AQAB), so e is fixed for keys we generate.
    """
    # timeout: openssl genrsa is sub-second even for 4096-bit keys; 60s is a
    # generous ceiling that still guarantees the tool can never hang (S#74).
    try:
        priv_pem = subprocess.run(
            ["openssl", "genrsa", str(bits)],
            capture_output=True, check=True, timeout=60,
        ).stdout.decode("ascii")
        modulus = subprocess.run(
            ["openssl", "rsa", "-noout", "-modulus"],
            input=priv_pem.encode("ascii"), capture_output=True, check=True,
            timeout=60,
        ).stdout.decode("ascii").strip()
    except FileNotFoundError:
        sys.exit("error: `openssl` not found on PATH — required to generate the keypair")
    except subprocess.TimeoutExpired:
        sys.exit(f"error: openssl timed out generating a {bits}-bit key")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: openssl failed: {exc.stderr.decode('utf-8', 'replace').strip()}")

    # `openssl rsa -modulus` prints `Modulus=<HEX>`.
    mod_hex = modulus.split("=", 1)[1]
    n_b64u = _b64u(bytes.fromhex(mod_hex))
    e_b64u = "AQAB"  # 65537 — openssl genrsa's fixed public exponent
    jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": _rfc7638_kid(n_b64u, e_b64u),
        "n": n_b64u,
        "e": e_b64u,
    }
    return priv_pem, jwk


def _secret_manifest(pem: str, name: str, namespace: str, key: str) -> str:
    """Render a Kubernetes Secret manifest carrying the private key.

    stringData (not data) — kubectl base64-encodes it on apply; the tool
    stays base64-free. The private key reaches etcd straight off the pipe.
    """
    indented = "".join(f"    {line}\n" for line in pem.splitlines())
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {namespace}\n"
        "  labels:\n"
        "    app.kubernetes.io/part-of: dynamic-alerting\n"
        "    app.kubernetes.io/component: federation-signing-key\n"
        "type: Opaque\n"
        "stringData:\n"
        f"  {key}: |\n"
        + indented
    )


def _merge_jwks(existing_path: str, new_jwk: dict) -> dict:
    """Append new_jwk to the JWKS at existing_path (dedupe by kid).

    Rotation overlap: both the old and new public keys live in the JWKS so
    tokens signed before the cutover keep verifying until they expire.
    """
    try:
        with open(existing_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: cannot read --existing-jwks {existing_path}: {exc}")
    keys = doc.get("keys")
    if not isinstance(keys, list):
        sys.exit(f"error: {existing_path} is not a JWKS document (missing `keys` array)")
    if any(k.get("kid") == new_jwk["kid"] for k in keys):
        sys.exit(f"error: a key with kid {new_jwk['kid']} is already in the JWKS "
                 "(the same keypair was generated twice?)")
    return {"keys": keys + [new_jwk]}


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="da-tools fed-key",
        description="Generate / rotate the federation JWT signing keypair (ADR-020 IV-2l).",
    )
    parser.add_argument("--rotate", action="store_true",
                        help="rotation mode — merge the new public key into --existing-jwks")
    parser.add_argument("--existing-jwks", metavar="PATH",
                        help="current JWKS file to merge into (required with --rotate)")
    parser.add_argument("--jwks-out", metavar="PATH", default="federation-jwks.json",
                        help="where to write the JWKS (default: ./federation-jwks.json)")
    parser.add_argument("--namespace", default="monitoring",
                        help="namespace for the Secret manifest (default: monitoring)")
    parser.add_argument("--secret-name", default="tenant-federation-signing-key",
                        help="name for the Secret (default: tenant-federation-signing-key)")
    parser.add_argument("--secret-key", default="federation-signing-key.pem",
                        help="data key inside the Secret (default: federation-signing-key.pem)")
    parser.add_argument("--key-bits", type=int, default=2048,
                        help="RSA modulus size (default: 2048; tenant-api requires >= 2048)")
    args = parser.parse_args()

    if args.rotate and not args.existing_jwks:
        parser.error("--rotate requires --existing-jwks")
    if args.key_bits < 2048:
        parser.error("--key-bits must be >= 2048 (tenant-api rejects weaker signing keys)")

    # TTY guard: the private-key Secret manifest goes to stdout for a pipe.
    # If stdout is an interactive terminal the operator forgot the `| kubectl`
    # — refuse, rather than printing the private key into their terminal
    # scrollback buffer where it lingers (a real leak the no-disk design is
    # meant to avoid). A pipe or `> file` redirect is not a tty, so the
    # documented invocations are unaffected.
    if sys.stdout.isatty():
        sys.exit(
            "error: refusing to write the private-key Secret manifest to a terminal.\n"
            "Pipe it straight to kubectl:  da-tools fed-key | kubectl apply -f -\n"
            "or redirect it to a file:     da-tools fed-key > signing-key.secret.yaml"
        )

    priv_pem, jwk = _generate_keypair(args.key_bits)

    if args.rotate:
        jwks = _merge_jwks(args.existing_jwks, jwk)
    else:
        jwks = {"keys": [jwk]}

    with open(args.jwks_out, "w", encoding="utf-8") as fh:
        json.dump(jwks, fh, indent=2)
        fh.write("\n")
    # JWKS carries only public keys — world-readable is correct (it ships in
    # the gateway's Helm values / git). Set 0o644 explicitly so the file's
    # permissions are intentional rather than umask-dependent.
    os.chmod(args.jwks_out, 0o644)

    # stdout: ONLY the Secret manifest, so `| kubectl apply -f -` is clean.
    sys.stdout.write(_secret_manifest(
        priv_pem, args.secret_name, args.namespace, args.secret_key))

    # stderr: operator guidance — never mixed into the piped manifest.
    print(f"\n[fed-key] new signing key, kid={jwk['kid']}", file=sys.stderr)
    print(f"[fed-key] private key  -> stdout as a Secret manifest "
          f"(namespace {args.namespace}) — pipe it to `kubectl apply -f -`",
          file=sys.stderr)
    print(f"[fed-key] public JWKS  -> {args.jwks_out} "
          f"({len(jwks['keys'])} key(s)) — set it as federation-gateway jwt.jwks",
          file=sys.stderr)
    if args.rotate:
        print("[fed-key] ROTATION: apply the JWKS to the gateway and wait for reload "
              "BEFORE applying this Secret to tenant-api — see the key-rotation runbook.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
