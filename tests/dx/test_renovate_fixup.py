"""Tests for renovate_fixup (#3799).

The program writes the copies a Renovate third-party image PR cannot reach.
These pin the pure rewrites (comments survive, only the named service moves,
a shape this program does not read is refused rather than guessed), and bind
its view of the deploy SoT to the guards that judge the result: if a guard
gains a mirrored service or reads a different source, these go red here
instead of the fixup silently falling short on the next Renovate PR.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import renovate_fixup as fx  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


def _load_test_module(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem + "_bound", _REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── binding to the guards ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def deployed() -> dict[str, str]:
    return fx.deployed_tags(_REPO)


def test_every_federation_mirror_the_guard_checks_is_planned_from_the_deploy_refs(deployed):
    """For each service the guard binds, the fixup's deploy reading gives the
    guard's own expected ref — so a fixup run leaves that guard green."""
    guard = _load_test_module("tests/ops/test_federation_e2e_mirrors_deploy_sot.py")
    for svc, read in guard.MIRRORED.items():
        image, _, tag = read().rpartition(":")
        assert deployed.get(image) == tag, (
            f"{svc}: the guard expects {image}:{tag}; renovate_fixup reads "
            f"{image}:{deployed.get(image)} from the deploy refs")


def test_federation_plan_moves_every_service_the_guard_checks(deployed):
    guard = _load_test_module("tests/ops/test_federation_e2e_mirrors_deploy_sot.py")
    compose = {"services": {svc: {"image": read().rpartition(":")[0] + ":v0-stale"}
                            for svc, read in guard.MIRRORED.items()}}
    assert set(fx.federation_plan(compose, deployed)) == set(guard.MIRRORED)


def test_proxy_tag_is_what_the_go_guard_reads(deployed):
    values = yaml.safe_load((_REPO / "helm/tenant-api/values.yaml").read_text(encoding="utf-8"))
    image = values["oauth2Proxy"]["image"]
    assert image["repository"] == fx.PROXY_IMAGE_REPO
    assert fx.proxy_chart_tag(deployed) == image["tag"]


def test_trylocal_rule_is_the_guards():
    guard = _load_test_module("tests/ops/test_nightly_scan_matrix_drift.py")
    assert fx._FIRST_PARTY_PREFIX == guard._FIRST_PARTY_PREFIX
    assert fx.TRY_LOCAL_COMPOSE == guard.TRY_LOCAL_COMPOSE.relative_to(_REPO).as_posix()


def test_the_repo_needs_no_fixup(deployed):
    """On a tree whose guards are green, every planner is a no-op."""
    fed = yaml.safe_load((_REPO / fx.FEDERATION_COMPOSE).read_text(encoding="utf-8"))
    assert fx.federation_plan(fed, deployed) == {}
    go_mod = (_REPO / fx.PROXY_LIB_TESTS_DIR / "go.mod").read_text(encoding="utf-8")
    assert fx.proxy_library_version(go_mod) == fx.proxy_chart_tag(deployed)
    trylocal = yaml.safe_load((_REPO / fx.TRY_LOCAL_COMPOSE).read_text(encoding="utf-8"))
    workflow = yaml.safe_load((_REPO / fx.SCAN_WORKFLOW).read_text(encoding="utf-8"))
    assert fx.trylocal_plan(trylocal, fx.scan_matrix_tags(workflow)) == {}
    images = json.loads((_REPO / fx.PLATFORM_DATA).read_text(encoding="utf-8"))["images"]
    _, changed = fx.set_image_mirror((_REPO / fx.IMAGES_JS).read_text(encoding="utf-8"), images)
    assert changed == {}


def test_federation_plan_leaves_images_the_platform_does_not_deploy():
    compose = {"services": {"double": {"image": "nginx:1.27-alpine"},
                            "prom": {"image": "prom/prometheus:v3.15.1"}}}
    assert fx.federation_plan(compose, {"prom/prometheus": "v3.16.0"}) == {
        "prom": ("prom/prometheus:v3.15.1", "prom/prometheus:v3.16.0")}


# ── set_compose_image ───────────────────────────────────────────────────────

_COMPOSE = """\
# header comment
services:
  prometheus:
    # why this version
    image: prom/prometheus:v3.15.1   # trailing
    command: ["--x"]
  other:
    image: "prom/prometheus:v3.15.1"
volumes:
  prometheus:
    driver: local
"""


def test_compose_rewrite_moves_only_the_named_service_and_keeps_comments():
    text, old = fx.set_compose_image(_COMPOSE, "prometheus", "prom/prometheus:v3.16.0")
    assert old == "prom/prometheus:v3.15.1"
    assert "    image: prom/prometheus:v3.16.0   # trailing\n" in text
    assert '    image: "prom/prometheus:v3.15.1"\n' in text  # `other` untouched
    assert "# why this version" in text and text.startswith("# header comment")
    assert yaml.safe_load(text)["volumes"] == {"prometheus": {"driver": "local"}}


def test_compose_rewrite_keeps_quotes():
    text, _ = fx.set_compose_image(_COMPOSE, "other", "x:1")
    assert '    image: "x:1"\n' in text


def test_compose_rewrite_ignores_a_volume_of_the_same_name():
    only_volume = "services:\n  a:\n    image: x:1\nvolumes:\n  b:\n    image: y:1\n"
    with pytest.raises(fx.FixupError, match="no service 'b'"):
        fx.set_compose_image(only_volume, "b", "y:2")


def test_compose_rewrite_refuses_a_service_without_image():
    with pytest.raises(fx.FixupError, match="has no image"):
        fx.set_compose_image("services:\n  a:\n    build: .\n", "a", "x:1")


# ── trylocal_plan ───────────────────────────────────────────────────────────

_MATRIX = {"prom/prometheus": {"v3.16.0"}, "curlimages/curl": {"8.23.0"},
           "two/tags": {"1", "2"}}


def test_trylocal_moves_a_stale_tag_to_the_matrix_tag():
    compose = {"services": {
        "prometheus": {"image": "prom/prometheus:v3.15.1"},
        "curl": {"image": "curlimages/curl:8.23.0"},
        "portal": {"image": "ghcr.io/vencil/da-portal:${PORTAL_TAG:-v2.8.0}"},
        "api": {"build": ".", "image": "local/api:source"},
    }}
    assert fx.trylocal_plan(compose, _MATRIX) == {
        "prometheus": ("prom/prometheus:v3.15.1", "prom/prometheus:v3.16.0")}


@pytest.mark.parametrize("image", ["two/tags:0", "not/scanned:1", "x/${V:-y}:1"])
def test_trylocal_refuses_what_it_cannot_decide(image):
    with pytest.raises(fx.FixupError):
        fx.trylocal_plan({"services": {"s": {"image": image}}}, _MATRIX)


# ── set_image_mirror ────────────────────────────────────────────────────────

_JS = """\
// lead
const IMAGE_MIRROR = {
  prometheus: 'prom/prometheus:v3.15.1',
  daPortal: 'ghcr.io/vencil/da-portal:v2.9.0',
};

const PLATFORM_IMAGES = { ...IMAGE_MIRROR };
"""


def test_mirror_rewrite_changes_only_values():
    text, changed = fx.set_image_mirror(_JS, {
        "prometheus": "prom/prometheus:v3.16.0", "daPortal": "ghcr.io/vencil/da-portal:v2.9.0"})
    assert changed == {"prometheus": ("prom/prometheus:v3.15.1", "prom/prometheus:v3.16.0")}
    assert text == _JS.replace("v3.15.1", "v3.16.0")


@pytest.mark.parametrize("images", [
    {"prometheus": "a:1"},                                         # mirror key missing in data
    {"prometheus": "a:1", "daPortal": "b:1", "newKey": "c:1"},     # data key missing in mirror
])
def test_mirror_rewrite_refuses_a_key_set_change(images):
    with pytest.raises(fx.FixupError):
        fx.set_image_mirror(_JS, images)


def test_proxy_library_version_reads_only_a_direct_require():
    assert fx.proxy_library_version(
        "require (\n\tgithub.com/oauth2-proxy/oauth2-proxy/v7 v7.15.6\n)\n") == "v7.15.6"
    with pytest.raises(fx.FixupError):
        fx.proxy_library_version(
            "require (\n\tgithub.com/oauth2-proxy/oauth2-proxy/v7 v7.15.6 // indirect\n)\n")


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["renovate_fixup.py", "--dry-run"])
    before = {rel: (_REPO / rel).read_bytes() for rel in (
        fx.FEDERATION_COMPOSE, fx.TRY_LOCAL_COMPOSE, fx.IMAGES_JS,
        f"{fx.PROXY_LIB_TESTS_DIR}/go.mod", f"{fx.DA_READ_DIR}/go.sum")}
    assert fx.main() == 0
    assert "nothing was written" in capsys.readouterr().out
    assert before == {rel: (_REPO / rel).read_bytes() for rel in before}


# ── #3981: the writes go through the artifact entry ────────────────────────

def test_a_refused_target_is_rc2_one_line(tmp_path, monkeypatch, capsys):
    """A write the entry refuses ends at `main` as rc 2 and one line naming
    the file, where the bare `write_text` raised into the crash handler.
    The go/make steps are stubbed; the image-mirror step runs for real on a
    copy whose mirror is stale."""
    from _lib_output import OutputWriteError
    repo = tmp_path / "repo"
    for rel in (fx.PLATFORM_DATA, fx.IMAGES_JS):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes((_REPO / rel).read_bytes())
    images = json.loads((repo / fx.PLATFORM_DATA).read_text(encoding="utf-8"))["images"]
    key = sorted(images)[0]
    images[key] = images[key] + "-stale-for-test"
    data = json.loads((repo / fx.PLATFORM_DATA).read_text(encoding="utf-8"))
    data["images"] = images
    (repo / fx.PLATFORM_DATA).write_text(json.dumps(data), encoding="utf-8")
    refused = []

    def refuse(path, text, **kw):
        refused.append(path)
        raise OutputWriteError(path, OSError(13, "Permission denied"))
    monkeypatch.setattr(fx, "write_repo_artifact", refuse)
    monkeypatch.setattr(fx, "_repo_root", lambda: repo)
    monkeypatch.setattr(fx.shutil, "which", lambda tool: "/bin/true")
    monkeypatch.setattr(fx, "_run", lambda *a, **k: None)
    for step in ("deployed_tags", "fix_proxy_library", "fix_federation_compose",
                 "fix_trylocal_compose"):
        monkeypatch.setattr(fx, step, lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["renovate_fixup.py"])
    before = (repo / fx.IMAGES_JS).read_bytes()
    with pytest.raises(SystemExit) as exc:
        fx.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert refused == [repo / fx.IMAGES_JS]
    assert err.strip().splitlines() == [
        f"ERROR: cannot write {repo / fx.IMAGES_JS}: Permission denied (errno 13)"
        " — internal output path, this is a bug or an unwritable workspace"], err
    assert (repo / fx.IMAGES_JS).read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFO on this platform")
def test_a_file_that_is_not_regular_is_refused_before_the_read(tmp_path):
    """A FIFO blocked the read forever; now it is a FixupError (rc 2)."""
    import signal
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("no SIGALRM on this platform")
    fifo = tmp_path / "compose.yaml"
    os.mkfifo(fifo)

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        with pytest.raises(fx.FixupError, match="not a regular file"):
            fx._read(fifo)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def test_a_file_that_is_not_utf8_is_a_fixup_error(tmp_path):
    """A UnicodeDecodeError is a ValueError: it went to the crash handler."""
    bad = tmp_path / "compose.yaml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(fx.FixupError, match="not UTF-8"):
        fx._read(bad)


def _stub_the_run(monkeypatch, repo):
    ran = []
    monkeypatch.setattr(fx, "_repo_root", lambda: repo)
    monkeypatch.setattr(fx.shutil, "which", lambda tool: "/bin/true")
    monkeypatch.setattr(fx, "_run", lambda cmd, *a, **k: ran.append(cmd))
    for step in ("deployed_tags", "fix_proxy_library", "fix_image_mirror"):
        monkeypatch.setattr(fx, step, lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["renovate_fixup.py"])
    return ran


def test_both_compose_files_are_computed_before_either_is_written(tmp_path, monkeypatch, capsys):
    """#3981: the federation compose was written before the try-local one was
    even read, so a try-local file that could not be read left the
    federation one changed. Now: rc 2 and neither is written."""
    repo = tmp_path / "repo"
    fed = repo / fx.FEDERATION_COMPOSE
    fed.parent.mkdir(parents=True, exist_ok=True)
    fed.write_text("old\n", encoding="utf-8")
    tl = repo / fx.TRY_LOCAL_COMPOSE
    tl.parent.mkdir(parents=True, exist_ok=True)
    tl.write_bytes(b"\xff\xfe")
    _stub_the_run(monkeypatch, repo)
    monkeypatch.setattr(fx, "fix_federation_compose", lambda *a, **k: (fed, "new\n"))
    assert fx.main() == 2
    assert "not UTF-8" in capsys.readouterr().err
    assert fed.read_text(encoding="utf-8") == "old\n"


def test_a_refused_target_stops_the_run_before_its_first_step(tmp_path, monkeypatch, capsys):
    """#3981: every file the run rewrites is judged before STEP 1 — here the
    image mirror is a directory, which the bare run met only at STEP 5,
    after the compose files had been rewritten and the go/make steps run."""
    repo = tmp_path / "repo"
    (repo / fx.IMAGES_JS).mkdir(parents=True)
    ran = _stub_the_run(monkeypatch, repo)
    wrote = []
    monkeypatch.setattr(fx, "fix_federation_compose", lambda *a, **k: wrote.append(1))
    with pytest.raises(SystemExit) as exc:
        fx.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot write {repo / fx.IMAGES_JS}: not a regular file"
        " — internal output path, this is a bug or an unwritable workspace"], err
    assert ran == [] and wrote == []


def test_the_compose_step_returns_its_rewrite_and_writes_nothing(tmp_path, deployed):
    """#3981: `fix_federation_compose` hands its rewrite back for `main` to
    write after the try-local one is computed; it writes nothing itself."""
    repo = tmp_path / "repo"
    fed = repo / fx.FEDERATION_COMPOSE
    fed.parent.mkdir(parents=True, exist_ok=True)
    fed.write_bytes((_REPO / fx.FEDERATION_COMPOSE).read_bytes())
    before = fed.read_bytes()
    bumped = {**deployed, "prom/prometheus": "v9.9.9"}
    got = fx.fix_federation_compose(repo, bumped, dry_run=False)
    assert got is not None and got[0] == fed and "prom/prometheus:v9.9.9" in got[1]
    assert fed.read_bytes() == before
    assert fx.fix_federation_compose(repo, bumped, dry_run=True) is None
