"""Does an image ref resolve? Asked of its registry through the distribution v2 API (#3611).

This replaced ``skopeo inspect`` / ``docker manifest inspect``, whose exit
status cannot tell "the registry has no such manifest" from "the registry
could not be reached": both exit non-zero, and reading which one it was from
the message text is the guess #3611 rules out. The registry's own answer is
structured, so it is read instead (owner ruling 2026-11-06, Q2 / Q3):

* ``200`` with a manifest media type (and, for a digest ref, that digest)
  → :data:`RESOLVED`;
* ``404`` ``MANIFEST_UNKNOWN`` / ``NAME_UNKNOWN`` → :data:`NOT_RESOLVED`;
* ``401`` ``UNAUTHORIZED`` after an anonymous token → :data:`NOT_RESOLVED`
  too: the images checked here must be pullable anonymously, and a registry
  answers a repository it does not have the same way it answers a private
  one (Docker Hub, quay.io, ghcr.io all measured 2026-11-11), so the message
  names both;
* ``429`` / ``TOOMANYREQUESTS``, a 5xx, no connection, a timeout, and any
  answer not listed above → :data:`NO_ANSWER`. An answer this module does not
  recognise is no verdict, never a finding.

A ``HEAD`` is sent first: Docker Hub does not count it against the anonymous
pull rate limit (a ``GET`` is counted). Only a ``HEAD`` that is not a clean
``200`` is followed by a ``GET``, because a ``HEAD`` reply has no body and so
no ``errors[].code`` to read.

Transport failures are :data:`_lib_prometheus.TRANSPORT_ERRORS`, the one list
of "no usable HTTP answer" (#3477), not an enumeration of its own.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, NamedTuple, Optional

from _lib_prometheus import TRANSPORT_ERRORS

RESOLVED, NOT_RESOLVED, NO_ANSWER = "resolved", "not-resolved", "no-answer"

#: What a manifest GET / HEAD may come back as: an image index or a single
#: manifest, OCI or Docker. Sent as ``Accept`` and required of a 200's
#: ``Content-Type`` — a 200 that is not one of these (a captive portal, a
#: proxy's HTML page) did not come from a registry and proves nothing.
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)

#: ``errors[].code`` values that mean the registry has no such manifest.
NOT_FOUND_CODES = frozenset({"MANIFEST_UNKNOWN", "NAME_UNKNOWN"})

DOCKER_HUB = "registry-1.docker.io"
#: Names a ref may give Docker Hub by; all are asked at DOCKER_HUB, and a
#: single-component name under any of them is under ``library/``.
DOCKER_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", DOCKER_HUB})

#: A digest as the OCI image spec writes it: ``<algorithm>:<encoded>``.
_DIGEST = re.compile(r"[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[a-zA-Z0-9=_-]+")

#: Digest algorithms whose bytes can be checked here (OCI image-spec names).
DIGEST_ALGORITHMS = frozenset({"sha256", "sha512"})


class Answer(NamedTuple):
    outcome: str        # RESOLVED / NOT_RESOLVED / NO_ANSWER
    reason: str         # one line for the report ("" when RESOLVED)


class InvalidRef(ValueError):
    """Text that is not an image reference (``repo:v1@``, ``repo:``). Input
    the check cannot read — the contract's 2 — not a verdict either way."""


class Ref(NamedTuple):
    host: str           # the registry's API host
    repository: str     # path under /v2/
    reference: str      # the digest when the ref carries one, else the tag


def parse_ref(ref: str) -> Ref:
    """Split *ref* the way a container runtime does.

    The first path component is a registry host only when it holds a ``.`` or
    a ``:`` or is ``localhost``; otherwise the ref is on Docker Hub, and a
    single-component name is under ``library/`` — also when Docker Hub is
    named explicitly (``docker.io/busybox``, ``index.docker.io/busybox``). A
    ref carrying both a tag and a digest (``repo:tag@sha256:…``, how #902 pins
    third-party images) is resolved by its digest — the tag is informational.
    """
    name, at, digest = ref.partition("@")
    if at and not _DIGEST.fullmatch(digest):
        raise InvalidRef(f"{ref!r}: '@' is not followed by an <algorithm>:<hex> digest")
    first, sep, rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = DOCKER_HUB, name
    if host in DOCKER_HUB_ALIASES:
        host = DOCKER_HUB
        if "/" not in path:
            path = f"library/{path}"
    repository, tag = path, "latest"
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        repository, _, tag = path.rpartition(":")
        if not tag:
            raise InvalidRef(f"{ref!r}: ':' is not followed by a tag")
    if not repository:
        raise InvalidRef(f"{ref!r}: no repository name")
    return Ref(host, repository, digest or tag)


#: (method, url, headers, timeout) → (status, headers, body). The seam the
#: tests replace; the default speaks HTTPS through urllib (and so honours
#: HTTPS_PROXY / the system CA store like every other urllib caller here).
Transport = Callable[[str, str, dict, float], tuple]


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a registry's redirect (registry.k8s.io answers 307 to its
    backend) only to https, and without the token on another host: urllib
    copies every header, Authorization included, and follows https → http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError(f"redirect to a non-https URL: {newurl}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).hostname
                                != urllib.parse.urlsplit(req.full_url).hostname):
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect)


def _urllib_transport(method: str, url: str, headers: dict, timeout: float) -> tuple:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read() if method != "HEAD" else b""
    except urllib.error.HTTPError as exc:
        body = b"" if method == "HEAD" else exc.read()
        return exc.code, exc.headers, body


def _bearer_challenge(headers) -> Optional[dict]:
    """The Bearer challenge among the reply's ``WWW-Authenticate`` headers —
    every one of them, not the first: a registry may offer Basic first."""
    for value in (headers.get_all("WWW-Authenticate") or []) if headers is not None else []:
        if value.lower().startswith("bearer "):
            return dict(re.findall(r'(\w+)="([^"]*)"', value))
    return None


class _NoAnswer(Exception):
    """A step that got no usable answer; becomes :data:`NO_ANSWER`."""


def _anonymous_token(challenge: dict, repository: str, timeout: float,
                     transport: Transport) -> Optional[str]:
    """An anonymous pull token, or None when the token service *refuses* one
    (401 / 403: ghcr.io answers 403 ``DENIED`` for a repository it does not
    have — the manifest request then gets its own 401, which is what is
    judged). Any other non-200 (a rate limit, a 5xx) is no answer: judging
    the manifest's tokenless 401 then would call a busy auth service a typo.
    """
    realm = challenge.get("realm")
    if not realm or not realm.startswith("https://"):
        return None
    query = {"scope": challenge.get("scope") or f"repository:{repository}:pull"}
    if challenge.get("service"):
        query["service"] = challenge["service"]
    status, _, body = transport("GET", f"{realm}?{urllib.parse.urlencode(query)}", {}, timeout)
    if status in (401, 403):
        return None
    if status != 200:
        raise _NoAnswer(f"HTTP {status} from the token service {realm}")
    payload = json.loads(body)
    token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        # A 200 without a token is not a refusal: going on without one would
        # judge the manifest's tokenless 401 as "not pullable anonymously".
        raise _NoAnswer(f"HTTP 200 from the token service {realm} without a token")
    return token


def _error_codes(body: bytes) -> list[str]:
    """``errors[].code`` of a registry error body; [] when it is not one."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return []
    return [e["code"] for e in errors if isinstance(e, dict) and isinstance(e.get("code"), str)]


def _content_type(headers) -> str:
    return (headers.get("Content-Type") or "").split(";", 1)[0].strip()


def _judge_ok(ref: Ref, headers, body: Optional[bytes]) -> Optional[Answer]:
    """A 200's proof that it is this manifest; None when the HEAD alone cannot
    tell (a digest ref whose reply carries no ``Docker-Content-Digest``)."""
    content_type = _content_type(headers)
    if content_type not in MANIFEST_TYPES:
        return Answer(NO_ANSWER, f"HTTP 200 but not a manifest (Content-Type {content_type or 'absent'})")
    algorithm, colon, _ = ref.reference.partition(":")
    if not colon:                       # a tag: a tag has no ':'
        return Answer(RESOLVED, "")
    if algorithm not in DIGEST_ALGORITHMS:
        return Answer(NO_ANSWER, f"cannot check a {algorithm} digest")
    if body is not None:
        got = f"{algorithm}:" + hashlib.new(algorithm, body).hexdigest()
    else:
        got = headers.get("Docker-Content-Digest")
        if not got:
            return None
    if got != ref.reference:
        return Answer(NO_ANSWER, f"HTTP 200 for a different manifest ({got})")
    return Answer(RESOLVED, "")


def _judge_error(ref: Ref, status: int, body: bytes) -> Answer:
    codes = _error_codes(body)
    detail = f"HTTP {status} {','.join(codes) or 'without an errors[].code'}"
    if status == 404 and NOT_FOUND_CODES.intersection(codes):
        return Answer(NOT_RESOLVED, f"{detail}: the registry has no such manifest")
    if status == 401 and "UNAUTHORIZED" in codes:
        return Answer(NOT_RESOLVED, f"{detail}: not pullable anonymously — the repository "
                                    f"name is wrong, or the image is private")
    if status == 429 or "TOOMANYREQUESTS" in codes:
        return Answer(NO_ANSWER, f"{detail}: rate limited")
    if 500 <= status <= 599:
        return Answer(NO_ANSWER, f"{detail}: registry error")
    return Answer(NO_ANSWER, f"{detail}: not an answer this check reads")


def resolve(ref: str, *, timeout: float, transport: Transport = _urllib_transport) -> Answer:
    """Ask *ref*'s registry whether it has that manifest. Never raises for a
    registry that did not answer: that is :data:`NO_ANSWER`."""
    try:
        parsed = parse_ref(ref)
    except InvalidRef as exc:
        return Answer(NO_ANSWER, f"not a valid image reference: {exc}")
    try:
        url = (f"https://{parsed.host}/v2/{parsed.repository}/manifests/"
               f"{urllib.parse.quote(parsed.reference, safe=':')}")
        headers = {"Accept": ", ".join(MANIFEST_TYPES)}
        status, reply, _ = transport("HEAD", url, headers, timeout)
        challenge = _bearer_challenge(reply) if status == 401 else None
        if challenge is not None:
            token = _anonymous_token(challenge, parsed.repository, timeout, transport)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                status, reply, _ = transport("HEAD", url, headers, timeout)
        if status == 200:
            verdict = _judge_ok(parsed, reply, None)
            if verdict is not None:
                return verdict
        elif status == 429 or 500 <= status <= 599:
            return Answer(NO_ANSWER, f"HTTP {status} on HEAD: "
                                     + ("rate limited" if status == 429 else "registry error"))
        status, reply, body = transport("GET", url, headers, timeout)
        if status == 200:
            return _judge_ok(parsed, reply, body) or Answer(NO_ANSWER, "HTTP 200 without a body")
        return _judge_error(parsed, status, body)
    except _NoAnswer as exc:
        return Answer(NO_ANSWER, str(exc))
    except (*TRANSPORT_ERRORS, RecursionError) as exc:
        # RecursionError: json.loads of a pathologically nested body.
        return Answer(NO_ANSWER, f"no answer from the registry: {type(exc).__name__}: {exc}")
