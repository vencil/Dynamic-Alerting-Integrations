"""Tests for scripts/tools/dx/release_publish.py (#3848).

The registry is the scripted transport of test_check_release_immutable.py
(no network); docker / cosign / helm / gh are a recorded fake of
`subprocess.run`, so every test can assert exactly which commands a decision
led to — above all, that a skip or a violation runs none of them.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# The fakes first: loading them registers check_release_immutable, which the
# wrapper then imports — one module object for both.
fx = _load("_immutable_fixtures", REPO_ROOT / "tests" / "dx" / "test_check_release_immutable.py")
rp = _load("release_publish", REPO_ROOT / "scripts" / "tools" / "dx" / "release_publish.py")

SHA, OTHER, CANDIDATE, HOST = fx.SHA, fx.OTHER, fx.CANDIDATE, fx.HOST
IMG = f"{HOST}/o/x"


class Commands:
    """Fake subprocess.run: records argv; answers `imagetools inspect` with
    the digest last written to that tag; `fail` maps argv[:2] → rc."""

    def __init__(self, fail=None, inspect=None, releases=None, assets=None, list_rc=0):
        self.calls: list[list[str]] = []
        self.tags: dict[str, str] = {}
        self.fail = fail or {}
        self.inspect = inspect
        self.releases = releases if releases is not None else []
        self.assets = assets
        self.list_rc = list_rc

    def __call__(self, argv, **kw):
        assert kw.get("timeout"), "every command needs a timeout"
        self.calls.append(list(argv))
        out = ""
        rc = self.fail.get(tuple(argv[:2]), 0)
        if argv[:4] == ["docker", "buildx", "imagetools", "create"]:
            self.tags[argv[5]] = argv[6].split("@", 1)[1]
        elif argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            out = self.inspect if self.inspect is not None else self.tags.get(argv[4], "")
        elif argv[:3] == ["gh", "release", "list"]:
            rc, out = self.list_rc, json.dumps([{"tagName": t} for t in self.releases])
        elif argv[:3] == ["gh", "release", "view"]:
            out = json.dumps({"assets": [a if isinstance(a, dict) else {"name": a, "state": "uploaded"}
                                         for a in self.assets or []]})
        return subprocess.CompletedProcess(argv, rc, out + "\n", "boom" if rc else "")

    def verbs(self) -> list[str]:
        return [" ".join(c[:4]) for c in self.calls]


@pytest.fixture(autouse=True)
def _fake_helm(monkeypatch):
    monkeypatch.setattr(rp.immutable, "HELM_SHOW_CHART", fx.fake_helm_show_chart)


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    for k in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    return out


def _run_image(monkeypatch, reg, cmds, *extra, tags=("v1.0.0", "latest")):
    monkeypatch.setattr(rp.immutable, "TRANSPORT", reg)
    monkeypatch.setattr(rp, "RUN", cmds)
    argv = ["image", "--image", IMG, "--digest", CANDIDATE, "--revision", SHA]
    for t in tags:
        argv += ["--tag", t]
    return rp.main(argv + list(extra))


# ── image ───────────────────────────────────────────────────────────────────

def test_first_publish_signs_then_writes_latest_first_and_the_version_last(env, monkeypatch, capsys):
    cmds = Commands()
    assert _run_image(monkeypatch, fx.Registry(), cmds, "--sign") == 0
    assert cmds.calls[0] == ["cosign", "sign", "--yes", f"{IMG}@{CANDIDATE}"]
    creates = [c[5] for c in cmds.calls if c[3:4] == ["create"]]
    assert creates == [f"{IMG}:latest", f"{IMG}:v1.0.0"], "the version must be written last"
    assert sum(c[3:4] == ["inspect"] for c in cmds.calls) == 2, "each tag is read back"
    assert env.read_text(encoding="utf-8") == f"publish=true\ndigest={CANDIDATE}\n"
    capsys.readouterr()


def test_without_sign_nothing_is_signed(env, monkeypatch, capsys):
    cmds = Commands()
    assert _run_image(monkeypatch, fx.Registry(), cmds) == 0
    assert not any(c[0] == "cosign" for c in cmds.calls)
    capsys.readouterr()


def test_already_published_from_this_commit_writes_nothing_and_leaves_latest(env, monkeypatch, capsys):
    reg = fx.Registry()
    index = fx._index(reg, [(fx._image(reg, SHA), fx.AMD)])
    cmds = Commands()
    assert _run_image(monkeypatch, reg, cmds, "--sign") == 0
    assert cmds.calls == [], "a skip must not sign, promote or touch latest"
    assert env.read_text(encoding="utf-8") == f"publish=false\ndigest={index}\n"
    capsys.readouterr()


@pytest.mark.parametrize("revision", [OTHER, None], ids=["other-commit", "no-label"])
def test_published_from_anything_else_is_rc_1_and_writes_nothing(env, monkeypatch, capsys, revision):
    reg = fx.Registry()
    fx._index(reg, [(fx._image(reg, revision), fx.AMD)])
    cmds = Commands()
    assert _run_image(monkeypatch, reg, cmds, "--sign") == 1
    assert cmds.calls == [] and env.read_text(encoding="utf-8") == ""
    capsys.readouterr()


def test_a_version_published_under_one_tag_but_not_another_is_rc_1(env, monkeypatch, capsys):
    reg = fx.Registry()
    fx._index(reg, [(fx._image(reg, SHA), fx.AMD)], tag="v1.0.0")
    cmds = Commands()
    assert _run_image(monkeypatch, reg, cmds, tags=("v1.0.0", "v1.0", "latest")) == 1
    assert cmds.calls == []
    capsys.readouterr()


def test_every_versioned_tag_is_checked_not_only_the_first(env, monkeypatch, capsys):
    reg = fx.Registry()
    fx._index(reg, [(fx._image(reg, OTHER), fx.AMD)], tag="v1")
    cmds = Commands()
    assert _run_image(monkeypatch, reg, cmds, tags=("v1.0.0", "v1", "latest")) == 1
    assert cmds.calls == []
    capsys.readouterr()


@pytest.mark.parametrize("cmds, why", [
    (lambda: Commands(fail={("cosign", "sign"): 1}), "sign fails"),
    (lambda: Commands(fail={("docker", "buildx"): 1}), "imagetools fails"),
    (lambda: Commands(inspect="sha256:" + "f" * 64), "read-back differs"),
], ids=["sign", "imagetools", "read-back"])
def test_a_failing_or_unverified_publish_is_rc_2(env, monkeypatch, capsys, cmds, why):
    c = cmds()
    assert _run_image(monkeypatch, fx.Registry(), c, "--sign") == 2, why
    assert env.read_text(encoding="utf-8") == ""
    if why == "sign fails":
        assert not any(x[:2] == ["docker", "buildx"] for x in c.calls), "promoted unsigned"
    capsys.readouterr()


def test_a_registry_without_an_answer_publishes_nothing(env, monkeypatch, capsys):
    reg = fx.Registry()
    reg.override["/manifests/v1.0.0"] = (503, fx._headers(), b"")
    cmds = Commands()
    assert _run_image(monkeypatch, reg, cmds) == 2 and cmds.calls == []
    capsys.readouterr()


@pytest.mark.parametrize("argv", [
    ["--tag", "latest"],                                   # no version at all
    ["--tag", "v1", "--tag", "v1"],
    ["--tag", "bad tag"],
], ids=["only-latest", "duplicate", "bad-tag"])
def test_unusable_tags_are_rc_2(env, monkeypatch, capsys, argv):
    monkeypatch.setattr(rp, "RUN", Commands())
    monkeypatch.setattr(rp.immutable, "TRANSPORT", fx.Registry())
    assert rp.main(["image", "--image", IMG, "--digest", CANDIDATE, "--revision", SHA, *argv]) == 2
    assert rp.main(["image", "--image", IMG + ":v1", "--digest", CANDIDATE, "--revision", SHA,
                    "--tag", "v1"]) == 2
    assert rp.main(["image", "--image", IMG, "--digest", "sha256:x", "--revision", SHA,
                    "--tag", "v1"]) == 2
    capsys.readouterr()


def test_a_new_repository_answered_as_measured_on_ghcr_is_a_first_publish(env, monkeypatch, capsys):
    """Measured 2026-11-13 (Actions run 43448765258, GITHUB_TOKEN with
    packages: write): for a repository that does not exist, the credentialed
    token request is 200 and the manifest 404 MANIFEST_UNKNOWN (not
    NAME_UNKNOWN). Anonymously the token service refuses (403 DENIED)."""
    monkeypatch.setenv("REGISTRY_USERNAME", "actor")
    monkeypatch.setenv("REGISTRY_PASSWORD", "token")
    reg = fx.Registry()
    cmds = Commands()
    assert _run_image(monkeypatch, fx._ghcr_new_repository(reg, code="MANIFEST_UNKNOWN"), cmds) == 0
    assert env.read_text(encoding="utf-8").startswith("publish=true\n")
    capsys.readouterr()


# ── chart ───────────────────────────────────────────────────────────────────

class Push:
    """Fake `helm push`: records the call and, unless *lands* is False,
    puts the archive into the fake registry the way a real push would."""

    def __init__(self, reg, lands=True):
        self.reg, self.lands, self.calls = reg, lands, []

    def __call__(self, tgz, oci_registry):
        self.calls.append([tgz, oci_registry])
        if self.lands:
            files = rp.immutable.chart_files(Path(tgz).read_bytes())
            fx._publish_chart(self.reg, files)
        return "Pushed\n"


def _run_chart(monkeypatch, tmp_path, reg, files, lands=True):
    monkeypatch.setattr(rp.immutable, "TRANSPORT", reg)
    push = Push(reg, lands)
    monkeypatch.setattr(rp, "HELM_PUSH", push)
    tgz = tmp_path / "demo-1.2.3+build.1.tgz"
    tgz.write_bytes(fx._tgz(files))
    return rp.main(["chart", "--tgz", str(tgz), "--registry", f"oci://{HOST}/o/charts"]), tgz, push


def test_chart_absent_is_pushed_and_read_back(env, monkeypatch, tmp_path, capsys):
    rc, tgz, push = _run_chart(monkeypatch, tmp_path, fx.Registry("o/charts/demo"), fx.BASE)
    assert rc == 0 and push.calls == [[str(tgz), f"oci://{HOST}/o/charts"]]
    assert env.read_text(encoding="utf-8") == "publish=true\n"
    assert "read back" in capsys.readouterr().out


def test_a_push_that_does_not_land_is_rc_2(env, monkeypatch, tmp_path, capsys):
    """helm exiting 0 is not the proof: the registry must hold the package after."""
    rc, _, push = _run_chart(monkeypatch, tmp_path, fx.Registry("o/charts/demo"), fx.BASE, lands=False)
    assert rc == 2 and len(push.calls) == 1 and env.read_text(encoding="utf-8") == ""
    capsys.readouterr()


def test_chart_already_published_the_same_is_not_pushed(env, monkeypatch, tmp_path, capsys):
    reg = fx.Registry("o/charts/demo")
    fx._publish_chart(reg, fx.BASE)
    rc, _, push = _run_chart(monkeypatch, tmp_path, reg, fx.BASE)
    assert rc == 0 and push.calls == [] and env.read_text(encoding="utf-8") == "publish=false\n"
    capsys.readouterr()


def test_chart_already_published_different_is_rc_1_and_not_pushed(env, monkeypatch, tmp_path, capsys):
    reg = fx.Registry("o/charts/demo")
    fx._publish_chart(reg, fx.BASE)
    rc, _, push = _run_chart(monkeypatch, tmp_path, reg, {**fx.BASE, "demo/values.yaml": b"x: 2\n"})
    assert rc == 1 and push.calls == []
    capsys.readouterr()


# ── Release assets ──────────────────────────────────────────────────────────

@pytest.fixture
def files(tmp_path):
    out = []
    for name in ("a.tar.gz", "SHA256SUMS", "a.tar.gz.sig"):
        p = tmp_path / name
        p.write_bytes(name.encode())
        out.append(str(p))
    return out


def _assets(monkeypatch, cmds, files):
    monkeypatch.setattr(rp, "RUN", cmds)
    return rp.main(["assets", "--tag", "tools/v1.0.0", "--title", "t", *files])


def test_no_release_creates_it_and_uploads_everything_without_clobber(env, monkeypatch, files, capsys):
    cmds = Commands(releases=[])
    assert _assets(monkeypatch, cmds, files) == 0
    assert cmds.calls[1][:4] == ["gh", "release", "create", "tools/v1.0.0"]
    assert cmds.calls[2] == ["gh", "release", "upload", "tools/v1.0.0", *files]
    assert not any("--clobber" in c for c in cmds.calls)
    capsys.readouterr()


def test_a_release_with_none_of_the_assets_gets_them_all(env, monkeypatch, files, capsys):
    cmds = Commands(releases=["tools/v1.0.0"], assets=["notes.md"])
    assert _assets(monkeypatch, cmds, files) == 0
    assert cmds.calls[-1][:3] == ["gh", "release", "upload"]
    assert not any(c[:3] == ["gh", "release", "create"] for c in cmds.calls)
    capsys.readouterr()


def test_a_release_with_all_assets_is_left_alone(env, monkeypatch, files, capsys):
    cmds = Commands(releases=["tools/v1.0.0"], assets=["a.tar.gz", "SHA256SUMS", "a.tar.gz.sig", "x"])
    assert _assets(monkeypatch, cmds, files) == 0
    assert not any(c[:3] == ["gh", "release", "upload"] for c in cmds.calls)
    assert env.read_text(encoding="utf-8") == "publish=false\n"
    capsys.readouterr()


def test_a_partial_asset_set_is_rc_1_and_uploads_nothing(env, monkeypatch, files, capsys):
    cmds = Commands(releases=["tools/v1.0.0"], assets=["a.tar.gz"])
    assert _assets(monkeypatch, cmds, files) == 1
    assert not any(c[:3] == ["gh", "release", "upload"] for c in cmds.calls)
    assert "delete-asset" in capsys.readouterr().err


def test_assets_that_cannot_be_listed_or_read_are_rc_2(env, monkeypatch, files, capsys):
    assert _assets(monkeypatch, Commands(list_rc=1), files) == 2
    assert _assets(monkeypatch, Commands(releases=[f"t{i}" for i in range(1000)]), files) == 2
    assert _assets(monkeypatch, Commands(), [*files, files[0] + ".missing"]) == 2
    assert _assets(monkeypatch, Commands(), [*files, files[0]]) == 2   # a name twice
    capsys.readouterr()


def test_an_asset_whose_upload_did_not_complete_is_not_present(env, monkeypatch, files, capsys):
    """gh lists an interrupted upload with a state other than `uploaded`."""
    cmds = Commands(releases=["tools/v1.0.0"],
                    assets=["a.tar.gz", "SHA256SUMS", {"name": "a.tar.gz.sig", "state": "starter"}])
    assert _assets(monkeypatch, cmds, files) == 1
    assert not any(c[:3] == ["gh", "release", "upload"] for c in cmds.calls)
    assert "not fully uploaded" in capsys.readouterr().err
