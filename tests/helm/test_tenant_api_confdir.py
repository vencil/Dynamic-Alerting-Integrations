"""helm/tenant-api conf.d volume: emptyDir or a PVC, never hostPath (#1018).

hostPath was the only chart option that fails even Pod Security `baseline`, and
with this chart's defaults it could not run (fsGroup does not apply to a host
directory; git refuses a repository another uid owns). Its replacement for "a
repository already on the node" is a PV the operator makes, bound to a PVC named
in confDir.pvc.existingClaim — the pod then mounts a PVC only.

What these tests pin:
- the schema's confDir.type enum equals the template's branches (plus the
  "hostPath" tombstone that exists only to reach the migration message), so the
  two cannot drift into a value one accepts and the other silently turns into
  emptyDir — which is what `confDir.type=PVC` did before (rc 0, emptyDir);
- the removed value fails with migration steps, not a bare schema error, and a
  values file started from the old `helm show values` (hostPath: "") renders;
- existingClaim mounts that claim and the chart creates no PVC of its own.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
# ⛔ One literal "helm/tenant-api" string (verify_diff.py maps tests to sources
# by scanning for literal paths).
_CHART = _ROOT / "helm/tenant-api"
_BASELINE = _ROOT / "tests/helm/tenant_api_render_baseline.yaml"
_TOMBSTONE = "hostPath"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


def _render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "t", str(_CHART), "-f", str(_BASELINE), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


def _docs(stdout: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(stdout) if isinstance(d, dict)]


def _conf_volume(stdout: str) -> dict:
    dep = next(d for d in _docs(stdout) if d.get("kind") == "Deployment")
    return next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "conf-dir")


def test_schema_enum_equals_the_template_branches() -> None:
    enum = set(json.loads((_CHART / "values.schema.json").read_text(encoding="utf-8"))
               ["properties"]["confDir"]["properties"]["type"]["enum"])
    branches: set[str] = set()
    for tpl in (_CHART / "templates").glob("*"):
        branches |= set(re.findall(r'eq \.Values\.confDir\.type "([^"]+)"', tpl.read_text(encoding="utf-8")))
    assert {"pvc", _TOMBSTONE} <= branches, f"anti-vacuity: branches read {branches}"
    # emptyDir is the template's `else`; it is the only legal value with no `eq`.
    assert enum == branches | {"emptyDir"}, (enum, branches)
    default = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))["confDir"]["type"]
    assert default in enum - {_TOMBSTONE}


@pytest.mark.parametrize("args", [
    ("--set", "confDir.type=hostPath", "--set", "confDir.hostPath=/srv/conf.d"),
    ("--set", "confDir.type=hostPath"),
    ("--set", "confDir.hostPath=/srv/conf.d"),  # type pvc (baseline) but the old key set
], ids=["type-and-path", "type-only", "path-only"])
def test_removed_hostpath_fails_with_the_migration_steps(args) -> None:
    out = _render(*args)
    assert out.returncode != 0
    assert "were removed (#1018)" in out.stderr and "confDir.pvc.existingClaim" in out.stderr, out.stderr[-600:]


@pytest.mark.parametrize("args", [
    ("--set", "confDir.type=PVC"),
    ("--set", "confDir.type=PVC", "--set", "gitRepoUrl=https://git.example.com/o/c.git"),
    ("--set", "confDir.hostpath=/x"),
], ids=["type-typo", "type-typo-with-clone", "key-typo"])
def test_a_typo_is_refused_not_turned_into_emptydir(args) -> None:
    out = _render(*args)
    assert out.returncode != 0, "a misspelled confDir value rendered (it used to become emptyDir)"


def test_values_started_from_the_old_helm_show_values_still_render(tmp_path: Path) -> None:
    old = {"confDir": {"type": "pvc", "hostPath": "",
                       "pvc": {"storageClass": "", "size": "1Gi", "accessMode": "ReadWriteOnce"}}}
    path = tmp_path / "old-values.yaml"
    path.write_text(yaml.safe_dump(old) + "\n", encoding="utf-8")
    out = _render("-f", str(path))
    assert out.returncode == 0, out.stderr[-600:]


def test_existing_claim_without_type_pvc_is_refused_not_ignored() -> None:
    out = _render("--set", "confDir.type=emptyDir", "--set", "gitRepoUrl=https://git.example.com/o/c.git",
                  "--set", "confDir.pvc.existingClaim=my-repo-pvc")
    assert out.returncode != 0 and "only read with confDir.type=pvc" in out.stderr, out.stderr[-600:]


def test_emptydir_creates_no_pvc() -> None:
    out = _render("--set", "confDir.type=emptyDir", "--set", "gitRepoUrl=https://git.example.com/o/c.git")
    assert out.returncode == 0, out.stderr[-600:]
    assert _conf_volume(out.stdout) == {"name": "conf-dir", "emptyDir": {}}
    assert not [d for d in _docs(out.stdout) if d.get("kind") == "PersistentVolumeClaim"]


def test_size_is_a_quantity_string() -> None:
    out = _render("--set-json", 'confDir.pvc.size=10')
    assert out.returncode != 0, "a bare number size (bytes) rendered"


def test_existing_claim_is_mounted_and_no_pvc_is_created() -> None:
    out = _render("--set", "confDir.pvc.existingClaim=conf-repo")
    assert out.returncode == 0, out.stderr[-600:]
    assert _conf_volume(out.stdout) == {"name": "conf-dir", "persistentVolumeClaim": {"claimName": "conf-repo"}}
    assert not [d for d in _docs(out.stdout) if d.get("kind") == "PersistentVolumeClaim"]


def test_pvc_without_existing_claim_creates_and_mounts_its_own() -> None:
    out = _render()
    assert out.returncode == 0, out.stderr[-600:]
    claim = _conf_volume(out.stdout)["persistentVolumeClaim"]["claimName"]
    pvcs = [d["metadata"]["name"] for d in _docs(out.stdout) if d.get("kind") == "PersistentVolumeClaim"]
    assert pvcs == [claim]
