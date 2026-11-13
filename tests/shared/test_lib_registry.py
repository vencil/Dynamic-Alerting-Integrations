"""_lib_registry: a ref resolves only on the registry's own structured answer (#3611).

The transport is replaced by a scripted one, so every branch runs without a
network. The replies scripted here are the shapes measured against the live
registries on 2026-11-11/12 (Docker Hub, quay.io, gcr.io, registry.k8s.io,
ghcr.io): see the module docstring of ``_lib_registry`` for which is which.
"""
from __future__ import annotations

import email.message
import hashlib
import json
import sys
import urllib.request

import pytest

from _tree import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_registry as R  # noqa: E402

INDEX = "application/vnd.oci.image.index.v1+json"
CHALLENGE = ('Bearer realm="https://auth.example/token",'
             'service="registry.example",scope="repository:o/p:pull"')


def _headers(*pairs, **fields) -> email.message.Message:
    msg = email.message.Message()
    for name, value in [*pairs, *fields.items()]:
        msg[name.replace("_", "-")] = value
    return msg


def _errors(*codes: str) -> bytes:
    return json.dumps({"errors": [{"code": c, "message": c.lower()} for c in codes]}).encode()


class Script:
    """A transport answering from a list of (method, status, headers, body),
    in order, and recording what was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, timeout):
        self.asked.append((method, url, dict(headers)))
        want, status, reply, body = self.replies.pop(0)
        assert method == want, (method, url, self.asked)
        return status, reply, body


def _ok_head(**extra):
    return ("HEAD", 200, _headers(Content_Type=INDEX, **extra), b"")


# ── parse_ref ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, want", [
    ("busybox:1.38", ("registry-1.docker.io", "library/busybox", "1.38")),
    ("busybox", ("registry-1.docker.io", "library/busybox", "latest")),
    ("prom/prometheus:v3", ("registry-1.docker.io", "prom/prometheus", "v3")),
    ("docker.io/prom/prometheus:v3", ("registry-1.docker.io", "prom/prometheus", "v3")),
    ("docker.io/busybox:1.38", ("registry-1.docker.io", "library/busybox", "1.38")),
    ("index.docker.io/busybox:1.38", ("registry-1.docker.io", "library/busybox", "1.38")),
    ("index.docker.io/prom/prometheus:v3", ("registry-1.docker.io", "prom/prometheus", "v3")),
    ("quay.io/o/p:v1@sha256:abc", ("quay.io", "o/p", "sha256:abc")),
    ("localhost:5000/x:1", ("localhost:5000", "x", "1")),
    ("localhost/x:1", ("localhost", "x", "1")),
    ("registry.k8s.io/a/b/c:v2", ("registry.k8s.io", "a/b/c", "v2")),
    ("mariadb:11.8.9@sha256:c41", ("registry-1.docker.io", "library/mariadb", "sha256:c41")),
])
def test_parse_ref_splits_like_a_container_runtime(ref, want):
    """A ref with both a tag and a digest is resolved by its digest (#902 L2
    pins `repo:tag@digest`); a first component without `.` / `:` is Docker Hub."""
    assert tuple(R.parse_ref(ref)) == want


# ── resolved ────────────────────────────────────────────────────────────────

def test_a_clean_head_is_resolved_without_a_get():
    """HEAD is not counted against Docker Hub's anonymous pull limit; a GET
    is, so a clean HEAD must be the whole conversation."""
    t = Script(_ok_head())
    assert R.resolve("quay.io/o/p:v1", timeout=5, transport=t) == (R.RESOLVED, "")
    assert [m for m, _, _ in t.asked] == ["HEAD"]


def test_an_anonymous_token_is_fetched_on_a_bearer_challenge():
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 200, _headers(), json.dumps({"token": "tok"}).encode()),
               _ok_head())
    assert R.resolve("example.io/o/p:v1", timeout=5, transport=t).outcome == R.RESOLVED
    token_url = t.asked[1][1]
    assert token_url.startswith("https://auth.example/token?")
    assert "service=registry.example" in token_url and "scope=repository%3Ao%2Fp%3Apull" in token_url
    assert t.asked[2][2]["Authorization"] == "Bearer tok"


def test_a_digest_ref_is_resolved_only_when_the_registry_names_that_digest():
    digest = "sha256:" + "a" * 64
    t = Script(_ok_head(Docker_Content_Digest=digest))
    assert R.resolve(f"quay.io/o/p:v1@{digest}", timeout=5, transport=t).outcome == R.RESOLVED
    other = "sha256:" + "b" * 64
    t = Script(_ok_head(Docker_Content_Digest=other))
    answer = R.resolve(f"quay.io/o/p@{digest}", timeout=5, transport=t)
    assert answer.outcome == R.NO_ANSWER and other in answer.reason


def test_a_digest_head_without_a_digest_header_is_settled_by_hashing_the_body():
    body = b'{"schemaVersion":2}'
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    t = Script(_ok_head(), ("GET", 200, _headers(Content_Type=INDEX), body))
    assert R.resolve(f"quay.io/o/p@{digest}", timeout=5, transport=t).outcome == R.RESOLVED
    t = Script(_ok_head(), ("GET", 200, _headers(Content_Type=INDEX), b"{}"))
    assert R.resolve(f"quay.io/o/p@{digest}", timeout=5, transport=t).outcome == R.NO_ANSWER


def test_a_200_that_is_not_a_manifest_proves_nothing():
    """A proxy's or captive portal's HTML page answers 200 too."""
    t = Script(("HEAD", 200, _headers(Content_Type="text/html; charset=utf-8"), b""))
    answer = R.resolve("quay.io/o/p:v1", timeout=5, transport=t)
    assert answer.outcome == R.NO_ANSWER and "not a manifest" in answer.reason


# ── not resolved (rc 1) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["MANIFEST_UNKNOWN", "NAME_UNKNOWN"])
def test_a_404_naming_no_such_manifest_is_not_resolved(code):
    t = Script(("HEAD", 404, _headers(), b""), ("GET", 404, _headers(), _errors(code)))
    answer = R.resolve("quay.io/o/p:nope", timeout=5, transport=t)
    assert answer.outcome == R.NOT_RESOLVED and code in answer.reason


def test_an_anonymous_401_is_not_resolved_naming_both_causes():
    """#3611 Q3: Docker Hub / quay / ghcr answer a repository they do not have
    exactly as they answer a private one."""
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 403, _headers(), _errors("DENIED")),         # ghcr's token reply
               ("GET", 401, _headers(), _errors("UNAUTHORIZED")))
    answer = R.resolve("example.io/o/typo:v1", timeout=5, transport=t)
    assert answer.outcome == R.NOT_RESOLVED
    assert "repository name is wrong" in answer.reason and "private" in answer.reason


# ── no answer (rc 2) ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_rate_limit_or_server_error_on_head_is_no_answer_without_a_get(status):
    t = Script(("HEAD", status, _headers(), b""))
    assert R.resolve("busybox:1.38", timeout=5, transport=t).outcome == R.NO_ANSWER
    assert len(t.asked) == 1


@pytest.mark.parametrize("status, body", [
    (429, _errors("TOOMANYREQUESTS")),
    (404, b"repository does not exist"),       # registry.k8s.io, missing repository
    (404, b""),
    (403, _errors("DENIED")),
    (400, _errors("MANIFEST_INVALID")),
    (200, b""),                                  # GET 200 that is not a manifest
], ids=["toomanyrequests", "404-plain-text", "404-empty", "403-denied", "400", "get-200-bare"])
def test_an_answer_this_module_does_not_read_is_no_answer(status, body):
    """Never a finding: only MANIFEST_UNKNOWN / NAME_UNKNOWN / anonymous
    UNAUTHORIZED are."""
    t = Script(("HEAD", 404, _headers(), b""), ("GET", status, _headers(), body))
    assert R.resolve("quay.io/o/p:v1", timeout=5, transport=t).outcome == R.NO_ANSWER


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), OSError("refused"),
                                 ValueError("bad url")])
def test_no_connection_is_no_answer(exc):
    def transport(*_):
        raise exc
    answer = R.resolve("quay.io/o/p:v1", timeout=5, transport=transport)
    assert answer.outcome == R.NO_ANSWER and type(exc).__name__ in answer.reason


def test_a_token_service_that_answers_garbage_is_no_answer():
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 200, _headers(), b"not json"))
    assert R.resolve("example.io/o/p:v1", timeout=5, transport=t).outcome == R.NO_ANSWER


def test_a_non_https_token_realm_is_not_followed():
    """The realm comes from the registry's reply; a token is fetched over
    https only. Without a token the manifest's own 401 is judged."""
    t = Script(("HEAD", 401, _headers(WWW_Authenticate='Bearer realm="http://x/t"'), b""),
               ("GET", 401, _headers(), _errors("UNAUTHORIZED")))
    assert R.resolve("example.io/o/p:v1", timeout=5, transport=t).outcome == R.NOT_RESOLVED
    assert all(not url.startswith("http://") for _, url, _ in t.asked)


# ── review findings on #3611 PR 4 ───────────────────────────────────────────

@pytest.mark.parametrize("token_status", [429, 500, 503])
def test_a_token_service_that_does_not_answer_is_no_answer_not_a_finding(token_status):
    """A rate-limited or failing auth service, then the manifest's tokenless
    401, is not "the repository name is wrong": no answer (2)."""
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", token_status, _headers(), _errors("TOOMANYREQUESTS")))
    answer = R.resolve("example.io/o/p:v1", timeout=5, transport=t)
    assert answer.outcome == R.NO_ANSWER and "token service" in answer.reason
    assert len(t.asked) == 2


@pytest.mark.parametrize("body", [b"", b"<html>401</html>", _errors("DENIED")])
def test_a_401_that_does_not_say_unauthorized_is_no_answer(body):
    """Only the registry's own UNAUTHORIZED is the anonymous-pull finding; a
    proxy's 401 page is not."""
    t = Script(("HEAD", 404, _headers(), b""), ("GET", 401, _headers(), body))
    assert R.resolve("quay.io/o/p:v1", timeout=5, transport=t).outcome == R.NO_ANSWER


def test_the_get_after_a_head_carries_the_token():
    """A digest HEAD with a token but no digest header is settled by a GET;
    without the token that GET is an anonymous 401 and a false finding."""
    body = b'{"schemaVersion":2}'
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 200, _headers(), json.dumps({"token": "tok"}).encode()),
               _ok_head(),
               ("GET", 200, _headers(Content_Type=INDEX), body))
    assert R.resolve(f"example.io/o/p@{digest}", timeout=5, transport=t).outcome == R.RESOLVED
    assert t.asked[3][2].get("Authorization") == "Bearer tok"


def test_a_pathologically_nested_body_is_no_answer_not_an_exception():
    t = Script(("HEAD", 404, _headers(), b""), ("GET", 404, _headers(), b"[" * 100000))
    assert R.resolve("quay.io/o/p:v1", timeout=5, transport=t).outcome == R.NO_ANSWER
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 200, _headers(), b"[" * 100000))
    assert R.resolve("example.io/o/p:v1", timeout=5, transport=t).outcome == R.NO_ANSWER


def test_a_bearer_challenge_after_a_basic_one_is_used():
    t = Script(("HEAD", 401, _headers(("WWW-Authenticate", 'Basic realm="x"'),
                                      ("WWW-Authenticate", CHALLENGE)), b""),
               ("GET", 200, _headers(), json.dumps({"token": "tok"}).encode()),
               _ok_head())
    assert R.resolve("example.io/o/p:v1", timeout=5, transport=t).outcome == R.RESOLVED


def test_a_sha512_digest_is_compared_too():
    want = "sha512:" + "a" * 128
    t = Script(_ok_head(Docker_Content_Digest="sha256:" + "0" * 64))
    assert R.resolve(f"quay.io/o/p@{want}", timeout=5, transport=t).outcome == R.NO_ANSWER
    t = Script(_ok_head(Docker_Content_Digest=want))
    assert R.resolve(f"quay.io/o/p@{want}", timeout=5, transport=t).outcome == R.RESOLVED


def test_a_digest_algorithm_this_module_cannot_hash_is_no_answer():
    t = Script(_ok_head(Docker_Content_Digest="blake3:" + "a" * 64))
    assert R.resolve("quay.io/o/p@blake3:" + "a" * 64, timeout=5,
                     transport=t).outcome == R.NO_ANSWER


# ── the real transport, against a local server ──────────────────────────────

class _Server:
    """A local HTTP server answering each path from a table:
    path → (status, headers, body)."""

    def __init__(self, table):
        import http.server
        import threading

        outer = self
        self.seen: list[tuple[str, str, dict]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self):
                outer.seen.append((self.command, self.path, dict(self.headers)))
                status, headers, body = table[self.path]
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_HEAD = _reply

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_urllib_transport_returns_error_replies_rather_than_raising():
    """HTTPError is an OSError: were it not caught first, every 404 / 401
    would be "no connection" and the gate would never find anything."""
    srv = _Server({"/m": (404, {"Content-Type": "application/json"}, _errors("MANIFEST_UNKNOWN")),
                   "/ok": (200, {"Content-Type": INDEX}, b"{}")})
    try:
        status, headers, body = R._urllib_transport("GET", srv.url + "/m", {}, 5)
        assert (status, R._error_codes(body)) == (404, ["MANIFEST_UNKNOWN"])
        status, headers, body = R._urllib_transport("HEAD", srv.url + "/m", {}, 5)
        assert (status, body) == (404, b"")
        status, headers, body = R._urllib_transport("GET", srv.url + "/ok", {}, 5)
        assert (status, headers.get("Content-Type"), body) == (200, INDEX, b"{}")
    finally:
        srv.close()


def test_a_redirect_to_plain_http_is_not_followed():
    """urllib follows https → http; a manifest proven over plain http is not."""
    req = urllib.request.Request("https://registry.example/v2/o/p/manifests/v1")
    with pytest.raises(OSError, match="non-https"):
        R._HttpsOnlyRedirect().redirect_request(req, None, 307, "", {},
                                                "http://registry.example/v2/o/p/manifests/v1")


def test_a_cross_host_redirect_drops_the_token_and_a_same_host_one_keeps_it():
    handler = R._HttpsOnlyRedirect()
    req = urllib.request.Request("https://registry.example/v2/o/p/manifests/v1",
                                 headers={"Authorization": "Bearer tok"})
    other = handler.redirect_request(req, None, 307, "", {}, "https://cdn.example/blob")
    same = handler.redirect_request(req, None, 307, "", {}, "https://registry.example/v2/x")
    assert not other.has_header("Authorization")
    assert same.get_header("Authorization") == "Bearer tok"


# ── CodeRabbit findings on #3757 ────────────────────────────────────────────

@pytest.mark.parametrize("ref", ["quay.io/o/p:v1@", "quay.io/o/p@", "quay.io/o/p:v1@sha256:",
                                 "quay.io/o/p@v1", "quay.io/o/p:", "quay.io/:v1"])
def test_text_that_is_not_an_image_reference_is_no_answer_and_asks_nothing(ref):
    """`repo:v1@` used to fall back to the tag and resolve it — rc 0 over a
    ref no runtime would pull. Unreadable input is 2, and no registry is asked."""
    t = Script()
    answer = R.resolve(ref, timeout=5, transport=t)
    assert answer.outcome == R.NO_ANSWER and "not a valid image reference" in answer.reason
    assert t.asked == []


@pytest.mark.parametrize("body", [b"{}", b"[]", b'{"token": 123}', b'{"token": ""}'])
def test_a_token_service_200_without_a_token_is_no_answer(body):
    """Going on tokenless would judge the manifest's 401 as a finding."""
    t = Script(("HEAD", 401, _headers(WWW_Authenticate=CHALLENGE), b""),
               ("GET", 200, _headers(), body))
    answer = R.resolve("example.io/o/p:v1", timeout=5, transport=t)
    assert answer.outcome == R.NO_ANSWER and "without a token" in answer.reason
    assert len(t.asked) == 2


# ── Repository: reading what a repository holds (#3848) ─────────────────────

MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def _repo(t, **kw):
    return R.Repository("ghcr.io", "o/p", timeout=5, transport=t, **kw)


@pytest.mark.parametrize("code", ["MANIFEST_UNKNOWN", "NAME_UNKNOWN"])
def test_repository_manifest_is_none_only_on_the_registrys_no_such_manifest(code):
    assert _repo(Script(("GET", 404, _headers(), _errors(code)))).manifest("v1") is None


@pytest.mark.parametrize("status, body", [
    (401, _errors("UNAUTHORIZED")), (403, _errors("DENIED")), (404, b""),
    (404, _errors("BLOB_UNKNOWN")), (429, b""), (500, b""), (503, _errors("UNAVAILABLE")),
    (200, b"<html>"),
], ids=["401", "403", "404-bare", "404-other", "429", "500", "503", "200-not-a-manifest"])
def test_repository_manifest_anything_else_is_no_answer(status, body):
    headers = _headers(Content_Type="text/html") if status == 200 else _headers()
    with pytest.raises(R.NoAnswer):
        _repo(Script(("GET", status, headers, body))).manifest("v1")


def test_repository_manifest_checks_its_bytes():
    body = b'{"schemaVersion":2}'
    good = "sha256:" + hashlib.sha256(body).hexdigest()
    got = _repo(Script(("GET", 200, _headers(Content_Type=MANIFEST, Docker_Content_Digest=good), body)))
    assert got.manifest("v1") == (MANIFEST, good, {"schemaVersion": 2})
    with pytest.raises(R.NoAnswer, match="different manifest"):
        _repo(Script(("GET", 200, _headers(Content_Type=MANIFEST), body))).manifest("sha256:" + "0" * 64)
    with pytest.raises(R.NoAnswer, match="not the sha256"):
        _repo(Script(("GET", 200, _headers(Content_Type=MANIFEST,
                                           Docker_Content_Digest="sha256:" + "0" * 64), body))).manifest("v1")
    with pytest.raises(R.NoAnswer, match="not a JSON object"):
        _repo(Script(("GET", 200, _headers(Content_Type=MANIFEST), b"[]"))).manifest("v1")


def test_repository_takes_an_anonymous_token_and_keeps_it():
    t = Script(("GET", 401, _headers(WWW_Authenticate=CHALLENGE), _errors("UNAUTHORIZED")),
               ("GET", 200, _headers(), json.dumps({"token": "tok"}).encode()),
               ("GET", 404, _headers(), _errors("MANIFEST_UNKNOWN")),
               ("GET", 404, _headers(), _errors("MANIFEST_UNKNOWN")))
    repo = _repo(t)
    assert repo.manifest("v1") is None and repo.manifest("v2") is None
    assert t.asked[2][2]["Authorization"] == "Bearer tok" == t.asked[3][2]["Authorization"]


def test_repository_without_credentials_reports_an_anonymous_refusal_as_no_answer():
    t = Script(("GET", 401, _headers(WWW_Authenticate=CHALLENGE), _errors("UNAUTHORIZED")),
               ("GET", 403, _headers(), _errors("DENIED")))
    with pytest.raises(R.NoAnswer, match="not readable anonymously"):
        _repo(t).manifest("v1")


def test_repository_blob_is_checked_against_digest_and_size():
    body = b"config"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    assert _repo(Script(("GET", 200, _headers(), body))).blob(digest, size=6, max_bytes=10) == body
    with pytest.raises(R.NoAnswer, match="do not hash"):
        _repo(Script(("GET", 200, _headers(), b"CONFIG"))).blob(digest, size=6, max_bytes=10)
    with pytest.raises(R.NoAnswer, match="descriptor says"):
        _repo(Script(("GET", 200, _headers(), body))).blob(digest, size=5, max_bytes=10)
    with pytest.raises(R.NoAnswer, match="descriptor size"):
        _repo(Script()).blob(digest, size=11, max_bytes=10)
    with pytest.raises(R.NoAnswer, match="cannot check a blake3"):
        _repo(Script(("GET", 200, _headers(), body))).blob("blake3:" + "a" * 64, max_bytes=10)
    with pytest.raises(R.NoAnswer):                      # a blob a manifest names must be there
        _repo(Script(("GET", 404, _headers(), _errors("BLOB_UNKNOWN")))).blob(digest, max_bytes=10)


def _tags(*tags, link=None):
    headers = _headers(Link=link) if link else _headers()
    return ("GET", 200, headers, json.dumps({"name": "o/p", "tags": list(tags)}).encode())


def test_repository_tags_follow_the_next_link_on_the_registry():
    t = Script(_tags("v1", link='</v2/o/p/tags/list?last=v1&n=1>; rel="next"'), _tags("v2"))
    assert _repo(t).tags(page_size=1) == ["v1", "v2"]
    assert t.asked[1][1] == "https://ghcr.io/v2/o/p/tags/list?last=v1&n=1"


def test_repository_tags_refuse_a_next_link_off_the_registry():
    t = Script(_tags("v1", link='<https://elsewhere.example/v2/o/p/tags/list?last=v1>; rel="next"'))
    with pytest.raises(R.NoAnswer, match="not on the registry"):
        _repo(t).tags()


def test_repository_tags_cannot_loop_forever(monkeypatch):
    monkeypatch.setattr(R, "MAX_TAG_PAGES", 3)
    t = Script(*[_tags("v1", link='</v2/o/p/tags/list?last=v1>; rel="next"')] * 3)
    with pytest.raises(R.NoAnswer, match="more than 3 pages"):
        _repo(t).tags()


def test_repository_tags_null_is_empty_and_a_missing_list_is_no_answer():
    assert _repo(Script(("GET", 200, _headers(), b'{"name":"o/p","tags":null}'))).tags() == []
    with pytest.raises(R.NoAnswer, match="no `tags` list"):
        _repo(Script(("GET", 200, _headers(), b'{"name":"o/p"}'))).tags()
    with pytest.raises(R.NoAnswer, match="not JSON"):
        _repo(Script(("GET", 200, _headers(), b"<html>"))).tags()
