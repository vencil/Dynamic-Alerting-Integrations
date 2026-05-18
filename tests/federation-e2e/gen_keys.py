#!/usr/bin/env python3
"""Generate a throwaway federation signing keypair for the E2E harness.

Writes  <rendered>/private-key.pem  — the driver RS256-signs federation
tokens with it — and  <rendered>/jwks.json  — the gateway's jwt_authn
verifies presented tokens against it. The key id is the RFC 7638 JWK
thumbprint, mirroring scripts/tools/ops/federation_keygen.py so the
token shape matches what tenant-api produces (ADR-020 IV-2j, #516).
"""
import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64u(raw: bytes) -> str:
    """Base64url, no padding (JWK encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_b64u(n: int) -> str:
    return _b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _rfc7638_kid(n_b64u: str, e_b64u: str) -> str:
    """RFC 7638 JWK thumbprint — canonical JSON (sorted keys, no
    whitespace) of the required RSA members, SHA-256, base64url. Mirrors
    federation_keygen.py::_rfc7638_kid."""
    canonical = json.dumps(
        {"e": e_b64u, "kty": "RSA", "n": n_b64u},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _b64u(hashlib.sha256(canonical).digest())


def main(rendered: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with open(f"{rendered}/private-key.pem", "wb") as fh:
        fh.write(pem)

    pub = key.public_key().public_numbers()
    n_b64u = _int_b64u(pub.n)
    e_b64u = _int_b64u(pub.e)
    kid = _rfc7638_kid(n_b64u, e_b64u)
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": kid,
                "n": n_b64u,
                "e": e_b64u,
            }
        ]
    }
    with open(f"{rendered}/jwks.json", "w", newline="\n") as fh:
        json.dump(jwks, fh, indent=2)

    print(f"[fed-e2e] generated throwaway keypair (kid={kid})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: gen_keys.py <rendered-dir>")
    main(sys.argv[1])
