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

Reading what a repository holds (#3848)
---------------------------------------
:class:`Repository` is the same client asked for content rather than a
yes/no: a manifest's bytes (:meth:`Repository.manifest`), a blob's bytes
(:meth:`Repository.blob`), the tag list (:meth:`Repository.tags`, following
``Link: rel="next"``). Every byte that comes back is checked against the
digest that names it, so "the registry holds X" is never read off a body
that is not X. Its contract is narrower than :func:`resolve`'s:

* only a ``404`` ``MANIFEST_UNKNOWN`` / ``NAME_UNKNOWN`` is "no such
  manifest" (``manifest()`` returns None);
* a ``401`` / ``403`` is :class:`NoAnswer`, never "absent": anonymously a
  registry answers a repository it does not have exactly as it answers a
  private one (ghcr.io: the token service refuses with ``403 DENIED`` either
  way, measured 2026-11-13), and a caller that must tell them apart gives
  :class:`Credentials`. Those are sent only after the anonymous attempt is
  refused, only as HTTP Basic to the registry's own token service (a realm on
  another host is refused, not followed), so a reply that a public repository
  gives anonymously stays the measured one;
* everything else — a 5xx, a 429, no connection, a body that is not what was
  asked for, bytes whose digest differs — is :class:`NoAnswer`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, NamedTuple, Optional

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


class NoAnswer(Exception):
    """A step that got no usable answer; :func:`resolve` turns it into
    :data:`NO_ANSWER`, :class:`Repository` raises it to its caller."""



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
        raise NoAnswer(f"HTTP {status} from the token service {realm}")
    payload = json.loads(body)
    token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        # A 200 without a token is not a refusal: going on without one would
        # judge the manifest's tokenless 401 as "not pullable anonymously".
        raise NoAnswer(f"HTTP 200 from the token service {realm} without a token")
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
    except NoAnswer as exc:
        return Answer(NO_ANSWER, str(exc))
    except (*TRANSPORT_ERRORS, RecursionError) as exc:
        # RecursionError: json.loads of a pathologically nested body.
        return Answer(NO_ANSWER, f"no answer from the registry: {type(exc).__name__}: {exc}")


# ── reading what a repository holds (#3848) ─────────────────────────────────


class Credentials(NamedTuple):
    """HTTP Basic credentials for a registry's token service."""
    username: str
    password: str


class Manifest(NamedTuple):
    media_type: str     # the reply's Content-Type, one of MANIFEST_TYPES
    digest: str         # sha256 of the bytes, as the registry names them
    document: dict      # the parsed JSON


#: A manifest larger than this is not read (the distribution spec lets a
#: registry refuse anything over 4 MiB, so no real manifest is near it).
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

#: Hard cap on a ``tags/list`` walk; a registry looping its ``Link`` header
#: would otherwise never end.
MAX_TAG_PAGES = 100

_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


def _digest_of(algorithm: str, body: bytes) -> str:
    if algorithm not in DIGEST_ALGORITHMS:
        raise NoAnswer(f"cannot check a {algorithm} digest")
    return f"{algorithm}:" + hashlib.new(algorithm, body).hexdigest()


def _refusal(status: int, body: bytes, credentials: Optional[Credentials]) -> str:
    codes = _error_codes(body)
    detail = f"HTTP {status} {','.join(codes) or 'without an errors[].code'}"
    if status in (401, 403):
        if credentials is None:
            return (f"{detail}: not readable anonymously — a registry answers a repository it "
                    f"does not have exactly as it answers a private one, so this is neither "
                    f"'absent' nor 'present' (give credentials to tell them apart)")
        return f"{detail}: refused with the given credentials"
    if status == 429 or "TOOMANYREQUESTS" in codes:
        return f"{detail}: rate limited"
    if 500 <= status <= 599:
        return f"{detail}: registry error"
    return f"{detail}: not an answer this reader reads"


class Repository:
    """One repository of one registry, read over the distribution v2 API.

    Raises :class:`NoAnswer` for every answer that is not the one asked for
    (see the module docstring); never returns a body it has not checked.
    """

    def __init__(self, host: str, repository: str, *, timeout: float,
                 transport: Transport = _urllib_transport,
                 credentials: Optional[Credentials] = None) -> None:
        self.host, self.repository, self.timeout = host, repository, timeout
        self.transport, self.credentials = transport, credentials
        self._token: Optional[str] = None
        self._challenge: Optional[dict] = None
        self._anonymous_tried = self._credentials_used = False

    @classmethod
    def of(cls, ref: str, **kwargs) -> tuple["Repository", str]:
        """(the repository *ref* names, its tag or digest)."""
        parsed = parse_ref(ref)
        return cls(parsed.host, parsed.repository, **kwargs), parsed.reference

    def _url(self, tail: str) -> str:
        return f"https://{self.host}/v2/{self.repository}/{tail}"

    def _credential_token(self) -> str:
        challenge = self._challenge or {}
        realm = challenge.get("realm") or ""
        if urllib.parse.urlsplit(realm).scheme != "https" \
                or urllib.parse.urlsplit(realm).hostname != self.host.split(":", 1)[0]:
            raise NoAnswer(f"refusing to send credentials to the token service {realm or '<none>'}: "
                           f"not https on the registry's own host {self.host}")
        query = {"scope": challenge.get("scope") or f"repository:{self.repository}:pull"}
        if challenge.get("service"):
            query["service"] = challenge["service"]
        basic = base64.b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode()).decode()
        status, _, body = self.transport("GET", f"{realm}?{urllib.parse.urlencode(query)}",
                                         {"Authorization": f"Basic {basic}"}, self.timeout)
        if status != 200:
            raise NoAnswer(f"HTTP {status} from the token service {realm} for the given credentials")
        payload = json.loads(body)
        token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise NoAnswer(f"HTTP 200 from the token service {realm} without a token")
        return token

    def _get(self, url: str, accept: Optional[str] = None) -> tuple:
        """GET *url*, authorising as the registry asks: an anonymous token
        first, credentials only once that is refused."""
        while True:
            headers = {"Accept": accept} if accept else {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            status, reply, body = self.transport("GET", url, headers, self.timeout)
            if status == 401:
                self._challenge = _bearer_challenge(reply) or self._challenge
            if status not in (401, 403) or self._challenge is None:
                return status, reply, body
            if not self._anonymous_tried:
                self._anonymous_tried = True
                token = _anonymous_token(self._challenge, self.repository, self.timeout,
                                         self.transport)
                if token:
                    self._token = token
                    continue
            if self.credentials is not None and not self._credentials_used:
                self._credentials_used = True
                self._token = self._credential_token()
                continue
            return status, reply, body

    def _ask(self, url: str, accept: Optional[str] = None) -> tuple:
        try:
            return self._get(url, accept)
        except NoAnswer:
            raise
        except (*TRANSPORT_ERRORS, RecursionError) as exc:
            raise NoAnswer(f"no answer from {self.host}: {type(exc).__name__}: {exc}") from exc

    def manifest(self, reference: str) -> Optional[Manifest]:
        """The manifest *reference* (tag or digest) names; None only when the
        registry says it has no such manifest."""
        url = self._url(f"manifests/{urllib.parse.quote(reference, safe=':')}")
        status, reply, body = self._ask(url, ", ".join(MANIFEST_TYPES))
        where = f"{self.host}/{self.repository}:{reference}"
        if status == 404 and NOT_FOUND_CODES.intersection(_error_codes(body)):
            return None
        if status != 200:
            raise NoAnswer(f"{where}: {_refusal(status, body, self.credentials)}")
        content_type = _content_type(reply)
        if content_type not in MANIFEST_TYPES:
            raise NoAnswer(f"{where}: HTTP 200 but not a manifest "
                           f"(Content-Type {content_type or 'absent'})")
        if len(body) > MAX_MANIFEST_BYTES:
            raise NoAnswer(f"{where}: a {len(body)}-byte manifest is not read")
        algorithm, colon, _ = reference.partition(":")
        if colon:                                   # a digest ref: the bytes must be it
            if _digest_of(algorithm, body) != reference:
                raise NoAnswer(f"{where}: HTTP 200 for a different manifest")
        claimed = reply.get("Docker-Content-Digest")
        if claimed and _digest_of(claimed.partition(":")[0], body) != claimed:
            raise NoAnswer(f"{where}: the body's digest is not the {claimed} the registry names")
        try:
            document = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise NoAnswer(f"{where}: manifest is not JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise NoAnswer(f"{where}: manifest is not a JSON object")
        return Manifest(content_type, _digest_of("sha256", body), document)

    def blob(self, digest: str, *, size: Optional[int] = None, max_bytes: int) -> bytes:
        """The blob *digest* names, checked against it (and against *size*,
        the descriptor's, when given). A blob a manifest names that is not
        there is no answer, not an absence."""
        where = f"{self.host}/{self.repository}@{digest}"
        if size is not None and (not isinstance(size, int) or size < 0 or size > max_bytes):
            raise NoAnswer(f"{where}: descriptor size {size!r} is not a size this reader reads "
                           f"(limit {max_bytes})")
        status, _, body = self._ask(self._url(f"blobs/{digest}"))
        if status != 200:
            raise NoAnswer(f"{where}: {_refusal(status, body, self.credentials)}")
        if len(body) > max_bytes or (size is not None and len(body) != size):
            raise NoAnswer(f"{where}: {len(body)} bytes, the descriptor says {size}")
        if _digest_of(digest.partition(":")[0], body) != digest:
            raise NoAnswer(f"{where}: the bytes do not hash to the digest that names them")
        return body

    def tags(self, page_size: int = 1000) -> List[str]:
        """Every tag of the repository, following ``Link: rel="next"`` on the
        registry's own host."""
        tags: List[str] = []
        url: Optional[str] = self._url(f"tags/list?n={page_size}")
        for _ in range(MAX_TAG_PAGES):
            status, reply, body = self._ask(url)
            if status != 200:
                raise NoAnswer(f"{self.host}/{self.repository} tags: "
                               f"{_refusal(status, body, self.credentials)}")
            try:
                payload = json.loads(body)
            except (ValueError, RecursionError) as exc:
                raise NoAnswer(f"{self.host}/{self.repository} tags: not JSON: {exc}") from exc
            page = payload.get("tags") if isinstance(payload, dict) else None
            if page is None and isinstance(payload, dict) and "tags" in payload:
                page = []                            # a repository with no tags says null
            if not isinstance(page, list) or not all(isinstance(t, str) for t in page):
                raise NoAnswer(f"{self.host}/{self.repository} tags: no `tags` list in the reply")
            tags.extend(page)
            match = _NEXT_LINK.search(reply.get("Link") or "")
            if not match:
                return tags
            url = urllib.parse.urljoin(f"https://{self.host}/", match.group(1))
            if urllib.parse.urlsplit(url).netloc != self.host or not url.startswith("https://"):
                raise NoAnswer(f"{self.host}/{self.repository} tags: next page {url} "
                               f"is not on the registry")
        raise NoAnswer(f"{self.host}/{self.repository} tags: more than {MAX_TAG_PAGES} pages")
