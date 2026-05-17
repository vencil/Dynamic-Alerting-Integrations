"""Tests for federation_keygen.py — federation JWT signing-key generation / rotation.

The load-bearing invariant is the `kid`: tenant-api stamps every token with
the RFC 7638 thumbprint of its signing key, and the gateway resolves the
JWKS key by that same `kid`. `test_rfc7638_kid_matches_published_vector`
pins the thumbprint algorithm to the RFC's worked example so the Python
side cannot silently drift from the Go signer (ADR-020 IV-2l, #518).
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import federation_keygen as fk  # noqa: E402

_HAS_OPENSSL = shutil.which("openssl") is not None
_needs_openssl = pytest.mark.skipif(not _HAS_OPENSSL, reason="openssl not on PATH")


# ---------------------------------------------------------------------------
# _b64u
# ---------------------------------------------------------------------------
def test_b64u_strips_padding():
    # 1 byte -> 2 base64 chars + "==" padding; padding must be gone.
    assert fk._b64u(b"\x00") == "AA"
    assert "=" not in fk._b64u(b"\x00\x01\x02\x03\x04")


def test_b64u_uses_url_safe_alphabet():
    # 0xfb 0xff encodes to "+/" in standard base64, "-_" in url-safe.
    encoded = fk._b64u(b"\xfb\xff")
    assert "+" not in encoded and "/" not in encoded


# ---------------------------------------------------------------------------
# _rfc7638_kid — the cross-component contract
# ---------------------------------------------------------------------------
# RFC 7638 Section 3.1 published worked example.
_RFC7638_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7a"
    "PFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArw"
    "l93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0z"
    "gdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2Nc"
    "Rwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)
_RFC7638_E = "AQAB"
_RFC7638_KID = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_rfc7638_kid_matches_published_vector():
    # If this fails, the thumbprint algorithm has drifted from the spec —
    # the gateway would no longer match tokens minted by tenant-api.
    assert fk._rfc7638_kid(_RFC7638_N, _RFC7638_E) == _RFC7638_KID


def test_rfc7638_kid_deterministic_and_key_specific():
    kid = fk._rfc7638_kid(_RFC7638_N, _RFC7638_E)
    assert fk._rfc7638_kid(_RFC7638_N, _RFC7638_E) == kid
    # A one-character change to the modulus yields a different kid.
    other_n = "A" + _RFC7638_N[1:]
    assert fk._rfc7638_kid(other_n, _RFC7638_E) != kid


# ---------------------------------------------------------------------------
# _secret_manifest
# ---------------------------------------------------------------------------
def test_secret_manifest_structure():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----\n"
    out = fk._secret_manifest(pem, "my-secret", "monitoring", "key.pem")
    assert "kind: Secret" in out
    assert "name: my-secret" in out
    assert "namespace: monitoring" in out
    # stringData (not data) — kubectl base64-encodes on apply; the tool
    # never base64s the private key itself.
    assert "stringData:" in out
    assert "\ndata:" not in out  # no top-level base64 `data:` block
    assert "  key.pem: |" in out
    # The PEM body is indented under the data key.
    assert "    -----BEGIN PRIVATE KEY-----" in out


# ---------------------------------------------------------------------------
# _merge_jwks
# ---------------------------------------------------------------------------
def _write_jwks(path, kids):
    path.write_text(json.dumps({"keys": [{"kid": k} for k in kids]}), encoding="utf-8")


def test_merge_jwks_appends_new_key(tmp_path):
    src = tmp_path / "jwks.json"
    _write_jwks(src, ["old-kid"])
    merged = fk._merge_jwks(str(src), {"kid": "new-kid"})
    assert [k["kid"] for k in merged["keys"]] == ["old-kid", "new-kid"]


def test_merge_jwks_rejects_duplicate_kid(tmp_path):
    src = tmp_path / "jwks.json"
    _write_jwks(src, ["dup-kid"])
    with pytest.raises(SystemExit):
        fk._merge_jwks(str(src), {"kid": "dup-kid"})


def test_merge_jwks_rejects_non_jwks_document(tmp_path):
    bad = tmp_path / "notjwks.json"
    bad.write_text(json.dumps({"not": "a jwks"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        fk._merge_jwks(str(bad), {"kid": "new-kid"})


def test_merge_jwks_rejects_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        fk._merge_jwks(str(tmp_path / "does-not-exist.json"), {"kid": "new-kid"})


# ---------------------------------------------------------------------------
# _generate_keypair — the JWK kid is self-consistent with _rfc7638_kid
# ---------------------------------------------------------------------------
@_needs_openssl
def test_generate_keypair_kid_is_rfc7638_thumbprint():
    priv_pem, jwk = fk._generate_keypair(2048)
    assert priv_pem.startswith("-----BEGIN")
    assert jwk["kty"] == "RSA" and jwk["e"] == "AQAB"
    # The kid embedded in the JWK must be the RFC 7638 thumbprint of its
    # own (n, e) — the same value tenant-api computes from the private key.
    assert jwk["kid"] == fk._rfc7638_kid(jwk["n"], jwk["e"])
    # SHA-256 base64url (no padding) is always 43 characters.
    assert len(jwk["kid"]) == 43


# ---------------------------------------------------------------------------
# main — end-to-end bootstrap & rotation
# ---------------------------------------------------------------------------
def test_main_rotate_without_existing_jwks_errors(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["fed-key", "--rotate"])
    with pytest.raises(SystemExit):
        fk.main()


def test_main_rejects_weak_key_bits(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["fed-key", "--key-bits", "1024"])
    with pytest.raises(SystemExit):
        fk.main()


@_needs_openssl
def test_main_bootstrap_emits_secret_and_jwks(tmp_path, monkeypatch, capsys):
    jwks_out = tmp_path / "federation-jwks.json"
    monkeypatch.setattr(sys, "argv", [
        "fed-key", "--jwks-out", str(jwks_out), "--namespace", "monitoring",
    ])
    rc = fk.main()
    assert rc == 0
    # Private key -> stdout as a Secret manifest.
    out = capsys.readouterr().out
    assert "kind: Secret" in out
    assert "namespace: monitoring" in out
    # Public key -> JWKS file with exactly one key.
    doc = json.loads(jwks_out.read_text(encoding="utf-8"))
    assert len(doc["keys"]) == 1
    assert len(doc["keys"][0]["kid"]) == 43


@_needs_openssl
def test_main_rotate_merges_into_existing_jwks(tmp_path, monkeypatch, capsys):
    jwks = tmp_path / "federation-jwks.json"
    # Bootstrap one key.
    monkeypatch.setattr(sys, "argv", ["fed-key", "--jwks-out", str(jwks)])
    assert fk.main() == 0
    first_kid = json.loads(jwks.read_text(encoding="utf-8"))["keys"][0]["kid"]
    capsys.readouterr()
    # Rotate: new key merged in alongside the first.
    monkeypatch.setattr(sys, "argv", [
        "fed-key", "--rotate", "--existing-jwks", str(jwks), "--jwks-out", str(jwks),
    ])
    assert fk.main() == 0
    keys = json.loads(jwks.read_text(encoding="utf-8"))["keys"]
    kids = [k["kid"] for k in keys]
    assert len(kids) == 2
    assert first_kid in kids
    assert len(set(kids)) == 2  # the rotation produced a distinct key
