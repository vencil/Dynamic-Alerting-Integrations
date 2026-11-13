"""Tests for scripts/tools/dx/check_release_immutable.py (#3848).

The registry is a scripted transport (_lib_registry's seam), so no network:
each test lays out what the registry holds — manifests by tag and digest,
blobs by digest — and the tool's decision is read back. The shapes are the
ones measured against ghcr.io on 2026-11-13 (an image index with
`unknown/unknown` attestation entries; a helm chart manifest whose one layer
is `application/vnd.cncf.helm.chart.content.v1.tar+gzip`).
"""
from __future__ import annotations

import email.message
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "dx" / "check_release_immutable.py"
_spec = importlib.util.spec_from_file_location("check_release_immutable", _SCRIPT)
tool = importlib.util.module_from_spec(_spec)
sys.modules["check_release_immutable"] = tool
assert _spec.loader is not None
_spec.loader.exec_module(tool)
R = tool.registry

SHA = "a" * 40
OTHER = "b" * 40
CANDIDATE = "sha256:" + "c" * 64
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
HOST = "ghcr.io"
CHALLENGE = f'Bearer realm="https://{HOST}/token",service="{HOST}",scope="repository:o/x:pull"'


def _headers(**fields) -> email.message.Message:
    msg = email.message.Message()
    for name, value in fields.items():
        msg[name.replace("_", "-")] = value
    return msg


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _errors(*codes: str) -> bytes:
    return json.dumps({"errors": [{"code": c} for c in codes]}).encode()


class Registry:
    """A transport serving one repository from dicts; records every request."""

    def __init__(self, repo: str = "o/x"):
        self.repo = repo
        self.manifests: dict[str, tuple[str, bytes]] = {}
        self.blobs: dict[str, bytes] = {}
        self.asked: list[tuple[str, str, dict]] = []
        self.override: dict[str, tuple] = {}     # url suffix → (status, headers, body)

    def put_blob(self, body: bytes) -> dict:
        d = _digest(body)
        self.blobs[d] = body
        return {"digest": d, "size": len(body)}

    def put_manifest(self, doc: dict, media_type: str, tag: str | None = None) -> str:
        body = json.dumps(doc).encode()
        d = _digest(body)
        self.manifests[d] = (media_type, body)
        if tag:
            self.manifests[tag] = (media_type, body)
        return d

    def __call__(self, method, url, headers, timeout):
        self.asked.append((method, url, dict(headers)))
        for suffix, reply in self.override.items():
            if url.endswith(suffix):
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        prefix = f"https://{HOST}/v2/{self.repo}/"
        assert url.startswith(prefix), url
        kind, _, ref = url[len(prefix):].partition("/")
        if kind == "manifests":
            if ref not in self.manifests:
                return 404, _headers(), _errors("MANIFEST_UNKNOWN")
            media_type, body = self.manifests[ref]
            return 200, _headers(Content_Type=media_type, Docker_Content_Digest=_digest(body)), body
        if kind == "blobs":
            if ref not in self.blobs:
                return 404, _headers(), _errors("BLOB_UNKNOWN")
            return 200, _headers(Content_Type="application/octet-stream"), self.blobs[ref]
        raise AssertionError(url)


def _image(reg: Registry, revision: str | None, labels_extra: dict | None = None) -> dict:
    labels = dict(labels_extra or {})
    if revision is not None:
        labels[tool.REVISION_LABEL] = revision
    config = reg.put_blob(json.dumps({"architecture": "amd64", "os": "linux",
                                      "config": {"Labels": labels or None}}).encode())
    doc = {"schemaVersion": 2, "mediaType": OCI_MANIFEST,
           "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **config},
           "layers": []}
    return {"mediaType": OCI_MANIFEST, "digest": reg.put_manifest(doc, OCI_MANIFEST), "size": 1}


def _index(reg: Registry, entries: list[tuple[dict, dict]], tag: str = "v1.0.0") -> str:
    manifests = [{**desc, "platform": platform} for desc, platform in entries]
    return reg.put_manifest({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests},
                            OCI_INDEX, tag)


AMD = {"os": "linux", "architecture": "amd64"}
ARM = {"os": "linux", "architecture": "arm64"}
ATTEST = {"os": "unknown", "architecture": "unknown"}


def _judge(reg: Registry, tag: str = "v1.0.0"):
    repo = R.Repository(HOST, reg.repo, timeout=5, transport=reg)
    return tool.judge_image(repo, tag, SHA, CANDIDATE)


# ── image ───────────────────────────────────────────────────────────────────

def test_image_first_publish_is_a_404():
    decision = _judge(Registry())
    assert decision.publish and decision.digest == CANDIDATE


def test_image_already_published_from_this_commit_is_skipped_on_its_digest():
    reg = Registry()
    attest = _image(reg, None)                       # attestations carry no label
    index = _index(reg, [(_image(reg, SHA), AMD), (_image(reg, SHA), ARM), (attest, ATTEST)])
    decision = _judge(reg)
    assert not decision.publish and decision.digest == index
    assert not any(attest["digest"] in url for _, url, _ in reg.asked), \
        "an attestation entry was read as a platform image"


def test_a_single_manifest_image_is_judged_too():
    reg = Registry()
    desc = _image(reg, SHA)
    reg.manifests["v1.0.0"] = reg.manifests[desc["digest"]]
    decision = _judge(reg)
    assert not decision.publish and decision.digest == desc["digest"]


def test_image_published_from_another_commit_is_a_violation():
    reg = Registry()
    _index(reg, [(_image(reg, OTHER), AMD), (_image(reg, OTHER), ARM)])
    with pytest.raises(tool.Violation, match="cannot be re-tagged"):
        _judge(reg)


def test_image_without_the_revision_label_is_a_violation():
    reg = Registry()
    _index(reg, [(_image(reg, None, {"org.opencontainers.image.version": "v1"}), AMD)])
    with pytest.raises(tool.Violation, match="no org.opencontainers.image.revision"):
        _judge(reg)


def test_one_platform_without_the_label_is_a_violation():
    reg = Registry()
    _index(reg, [(_image(reg, SHA), AMD), (_image(reg, None), ARM)])
    with pytest.raises(tool.Violation, match="linux/arm64"):
        _judge(reg)


def test_platforms_that_disagree_are_a_violation_even_if_one_is_this_commit():
    reg = Registry()
    _index(reg, [(_image(reg, SHA), AMD), (_image(reg, OTHER), ARM)])
    with pytest.raises(tool.Violation, match="disagree"):
        _judge(reg)


def test_an_index_of_attestations_only_is_no_answer():
    reg = Registry()
    _index(reg, [(_image(reg, None), ATTEST)])
    with pytest.raises(R.NoAnswer, match="no platform manifest"):
        _judge(reg)


def test_an_index_naming_a_manifest_the_registry_lacks_is_no_answer():
    reg = Registry()
    _index(reg, [({"mediaType": OCI_MANIFEST, "digest": "sha256:" + "d" * 64, "size": 1}, AMD)])
    with pytest.raises(R.NoAnswer, match="does not have"):
        _judge(reg)


@pytest.mark.parametrize("status, body", [
    (500, b""), (502, b"bad gateway"), (503, _errors("UNAVAILABLE")),
    (429, _errors("TOOMANYREQUESTS")), (404, b"not found"), (404, _errors("BLOB_UNKNOWN")),
    (400, _errors("MANIFEST_INVALID")),
], ids=["500", "502", "503", "429", "404-plain", "404-other-code", "400"])
def test_a_manifest_answer_that_is_not_present_or_absent_is_no_answer(status, body):
    reg = Registry()
    reg.override["/manifests/v1.0.0"] = (status, _headers(), body)
    with pytest.raises(R.NoAnswer):
        _judge(reg)


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), OSError("reset"), ValueError("x")])
def test_no_connection_is_no_answer(exc):
    reg = Registry()
    reg.override["/manifests/v1.0.0"] = exc
    with pytest.raises(R.NoAnswer, match=type(exc).__name__):
        _judge(reg)


def test_a_config_blob_that_does_not_match_its_digest_is_no_answer():
    reg = Registry()
    desc = _image(reg, SHA)
    _index(reg, [(desc, AMD)])
    config = json.loads(reg.manifests[desc["digest"]][1])["config"]["digest"]
    reg.blobs[config] = reg.blobs[config].replace(SHA.encode(), OTHER.encode())
    with pytest.raises(R.NoAnswer, match="do not hash"):
        _judge(reg)


def test_a_manifest_body_that_is_not_the_digest_the_registry_names_is_no_answer():
    reg = Registry()
    _index(reg, [(_image(reg, SHA), AMD)])
    media_type, body = reg.manifests["v1.0.0"]
    reg.override["/manifests/v1.0.0"] = (200, _headers(Content_Type=media_type,
                                                       Docker_Content_Digest="sha256:" + "e" * 64), body)
    with pytest.raises(R.NoAnswer, match="not the sha256"):
        _judge(reg)


def test_a_child_manifest_that_is_not_its_digest_is_no_answer():
    reg = Registry()
    desc = _image(reg, SHA)
    _index(reg, [(desc, AMD)])
    media_type, body = reg.manifests[desc["digest"]]
    reg.manifests[desc["digest"]] = (media_type, body + b" ")
    with pytest.raises(R.NoAnswer):
        _judge(reg)


# ── authorisation ───────────────────────────────────────────────────────────

def _ghcr_new_repository(reg: Registry, with_token_for_credentials: bool = True):
    """ghcr.io for a repository that does not exist (measured anonymously):
    401 + challenge, the token service refuses with 403 DENIED."""
    unauth = (401, _headers(WWW_Authenticate=CHALLENGE), _errors("UNAUTHORIZED"))

    def transport(method, url, headers, timeout):
        reg.asked.append((method, url, dict(headers)))
        if url.startswith(f"https://{HOST}/token"):
            if headers.get("Authorization", "").startswith("Basic ") and with_token_for_credentials:
                return 200, _headers(), json.dumps({"token": "cred-tok"}).encode()
            return 403, _headers(), _errors("DENIED")
        if headers.get("Authorization") == "Bearer cred-tok":
            return 404, _headers(), _errors("NAME_UNKNOWN")
        return unauth

    return transport


def test_an_anonymous_401_is_no_answer_not_a_first_publish():
    """A repository that does not exist and a private one answer alike."""
    reg = Registry()
    repo = R.Repository(HOST, "o/x", timeout=5, transport=_ghcr_new_repository(reg))
    with pytest.raises(R.NoAnswer, match="not readable anonymously"):
        tool.judge_image(repo, "v1.0.0", SHA, CANDIDATE)


def test_credentials_tell_a_new_repository_from_a_private_one():
    reg = Registry()
    repo = R.Repository(HOST, "o/x", timeout=5, transport=_ghcr_new_repository(reg),
                        credentials=R.Credentials("actor", "s3cret"))
    assert tool.judge_image(repo, "v1.0.0", SHA, CANDIDATE).publish
    token_asks = [h.get("Authorization", "") for _, u, h in reg.asked
                  if u.startswith(f"https://{HOST}/token")]
    assert token_asks[0] == "", "credentials were sent before the anonymous attempt was refused"
    assert [a.startswith("Basic ") for a in token_asks] == [False, True]


def test_credentials_refused_is_no_answer():
    reg = Registry()
    repo = R.Repository(HOST, "o/x", timeout=5,
                        transport=_ghcr_new_repository(reg, with_token_for_credentials=False),
                        credentials=R.Credentials("actor", "s3cret"))
    with pytest.raises(R.NoAnswer, match="for the given credentials"):
        tool.judge_image(repo, "v1.0.0", SHA, CANDIDATE)


def test_credentials_are_never_sent_to_a_token_service_on_another_host():
    asked = []

    def transport(method, url, headers, timeout):
        asked.append((url, dict(headers)))
        if url.startswith("https://evil.example/"):
            return 403, _headers(), _errors("DENIED")
        return 401, _headers(WWW_Authenticate='Bearer realm="https://evil.example/token"'), \
            _errors("UNAUTHORIZED")

    repo = R.Repository(HOST, "o/x", timeout=5, transport=transport,
                        credentials=R.Credentials("actor", "s3cret"))
    with pytest.raises(R.NoAnswer, match="refusing to send credentials"):
        repo.manifest("v1")
    assert not any("Basic" in h.get("Authorization", "") for _, h in asked)


# ── chart ───────────────────────────────────────────────────────────────────

CHART_YAML = b"apiVersion: v2\nname: demo\nversion: 1.2.3+build.1\nappVersion: \"1.2.3\"\n"


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


BASE = {"demo/Chart.yaml": CHART_YAML, "demo/values.yaml": b"replicas: 1\n",
        "demo/templates/deploy.yaml": b"kind: Deployment\n"}


def _publish_chart(reg: Registry, files: dict[str, bytes], tag: str = "1.2.3_build.1") -> None:
    layer = reg.put_blob(_tgz(files))
    reg.put_manifest({"schemaVersion": 2,
                      "config": {"mediaType": "application/vnd.cncf.helm.config.v1+json",
                                 **reg.put_blob(b"{}")},
                      "layers": [{"mediaType": tool.CHART_LAYER, **layer}]}, OCI_MANIFEST, tag)


def _judge_chart(reg: Registry, local: dict[str, bytes]):
    repo = R.Repository(HOST, reg.repo, timeout=5, transport=reg)
    return tool.judge_chart(repo, "1.2.3_build.1", tool.chart_files(_tgz(local)))


def test_chart_first_publish_is_a_404():
    assert _judge_chart(Registry("o/charts/demo"), BASE).publish


def test_the_same_chart_is_not_re_pushed():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    assert not _judge_chart(reg, BASE).publish


def test_a_chart_yaml_that_differs_only_in_formatting_and_comments_is_the_same():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    local = {**BASE, "demo/Chart.yaml": b"# helm re-serialises this file\nname: demo\n"
                                        b"apiVersion: 'v2'\nappVersion: '1.2.3'\n"
                                        b"version:   1.2.3+build.1\n"}
    assert not _judge_chart(reg, local).publish


def test_a_differing_file_is_a_violation_naming_it():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    local = {**BASE, "demo/values.yaml": b"replicas: 2\n", "demo/templates/new.yaml": b"x\n"}
    with pytest.raises(tool.Violation) as err:
        _judge_chart(reg, local)
    text = str(err.value)
    assert "demo/values.yaml" in text and "demo/templates/new.yaml (only in the new package)" in text
    assert "demo/templates/deploy.yaml" not in text and "demo/Chart.yaml" not in text


def test_a_chart_yaml_with_a_different_value_is_a_violation():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    local = {**BASE, "demo/Chart.yaml": CHART_YAML.replace(b'"1.2.3"', b'"1.2.4"')}
    with pytest.raises(tool.Violation, match="demo/Chart.yaml"):
        _judge_chart(reg, local)


def test_a_file_dropped_from_the_new_package_is_a_violation():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    local = {k: v for k, v in BASE.items() if "deploy" not in k}
    with pytest.raises(tool.Violation, match="only in the published package"):
        _judge_chart(reg, local)


def test_a_chart_blob_that_does_not_match_its_digest_is_no_answer():
    reg = Registry("o/charts/demo")
    _publish_chart(reg, BASE)
    layer = json.loads(reg.manifests["1.2.3_build.1"][1])["layers"][0]["digest"]
    reg.blobs[layer] = _tgz({**BASE, "demo/values.yaml": b"replicas: 9\n"})
    with pytest.raises(R.NoAnswer):
        _judge_chart(reg, BASE)


def test_a_manifest_without_a_chart_layer_is_no_answer():
    reg = Registry("o/charts/demo")
    reg.put_manifest({"schemaVersion": 2, "config": reg.put_blob(b"{}"), "layers": []},
                     OCI_MANIFEST, "1.2.3_build.1")
    with pytest.raises(R.NoAnswer, match="expected one"):
        _judge_chart(reg, BASE)


def test_an_archive_with_a_duplicate_member_is_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for data in (b"a", b"b"):
            info = tarfile.TarInfo("demo/values.yaml")
            info.size = 1
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="twice"):
        tool.chart_files(buf.getvalue())


# ── main ────────────────────────────────────────────────────────────────────

@pytest.fixture
def out(tmp_path, monkeypatch):
    path = tmp_path / "github_output"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.delenv("REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("REGISTRY_PASSWORD", raising=False)
    return path


def _main_image(monkeypatch, reg, *extra):
    monkeypatch.setattr(tool, "TRANSPORT", reg)
    return tool.main(["image", "--ref", f"{HOST}/o/x:v1.0.0", "--revision", SHA, *extra])


def test_main_writes_the_decision(out, monkeypatch, capsys):
    reg = Registry()
    assert _main_image(monkeypatch, reg, "--candidate-digest", CANDIDATE) == 0
    assert out.read_text(encoding="utf-8") == f"publish=true\ndigest={CANDIDATE}\n"
    out.write_text("", encoding="utf-8")
    index = _index(reg, [(_image(reg, SHA), AMD)])
    assert _main_image(monkeypatch, reg, "--candidate-digest", CANDIDATE) == 0
    assert out.read_text(encoding="utf-8") == f"publish=false\ndigest={index}\n"
    capsys.readouterr()


def test_main_exit_codes(out, monkeypatch, capsys):
    reg = Registry()
    _index(reg, [(_image(reg, OTHER), AMD)])
    assert _main_image(monkeypatch, reg) == 1
    reg.override["/manifests/v1.0.0"] = (503, _headers(), b"")
    assert _main_image(monkeypatch, reg) == 2
    assert out.read_text(encoding="utf-8") == "", "a failed check wrote a decision"
    monkeypatch.setattr(tool, "TRANSPORT", Registry())
    assert tool.main(["image", "--ref", f"{HOST}/o/x:v1", "--revision", "main"]) == 2
    assert tool.main(["image", "--ref", f"{HOST}/o/x@sha256:" + "a" * 64, "--revision", SHA]) == 2
    monkeypatch.setenv("REGISTRY_USERNAME", "only-one")
    assert tool.main(["image", "--ref", f"{HOST}/o/x:v1", "--revision", SHA]) == 2
    capsys.readouterr()


def test_main_chart(out, monkeypatch, tmp_path, capsys):
    reg = Registry("o/charts/demo")
    monkeypatch.setattr(tool, "TRANSPORT", reg)
    tgz = tmp_path / "demo-1.2.3+build.1.tgz"
    tgz.write_bytes(_tgz(BASE))
    args = ["chart", "--tgz", str(tgz), "--registry", f"oci://{HOST}/o/charts"]
    assert tool.main(args) == 0 and out.read_text(encoding="utf-8") == "publish=true\n"
    assert reg.asked[0][1].endswith("/o/charts/demo/manifests/1.2.3_build.1"), \
        "helm's OCI tag writes `+` as `_`"
    _publish_chart(reg, BASE)
    out.write_text("", encoding="utf-8")
    assert tool.main(args) == 0 and out.read_text(encoding="utf-8") == "publish=false\n"
    tgz.write_bytes(_tgz({**BASE, "demo/values.yaml": b"replicas: 3\n"}))
    assert tool.main(args) == 1
    tgz.write_bytes(b"not a tgz")
    assert tool.main(args) == 2
    assert tool.main(["chart", "--tgz", str(tgz), "--registry", "ghcr.io/o/charts"]) == 2
    capsys.readouterr()
