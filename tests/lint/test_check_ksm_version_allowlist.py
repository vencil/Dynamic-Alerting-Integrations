"""Tests for check_ksm_version_allowlist.py — ADR-024 KSM allowlist invariant.

Pinned contracts
----------------
1. **Pass**: a KSM container whose allowlist includes app.kubernetes.io/version
   (alone, alongside other pod labels, or with other resources) is compliant.
2. **Catch no-allowlist**: a KSM container with no --metric-labels-allowlist arg
   at all is flagged (the default-deployment silent-inert case).
3. **Catch partial-misconfig (Gemini Pass-4)**: an allowlist set to a DIFFERENT
   label only (pods=[app.kubernetes.io/managed-by]) is flagged — this is the
   exact gap the runtime sentinel cannot see.
4. **Live dogfood**: the committed k8s/ KSM Deployment allowlists the version
   label (gates a regression of the ADR-024 PR3b deployment fix).
5. **Readers decide (#3426)**: a KSM Deployment is what kubectl's reading of
   k8s/ holds, not what the text mentions; a file that reading refuses is
   named at rc 2, never passed over.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_ksm_version_allowlist as lint  # noqa: E402


def _ok(args):
    return lint.ksm_allowlist_ok(args)[0]


def test_version_allowlisted_alone():
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/version]"]) is True


def test_version_allowlisted_among_other_pod_labels():
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/version]"]) is True


def test_version_allowlisted_with_other_resources():
    assert _ok(["--metric-labels-allowlist=nodes=[kubernetes.io/role],pods=[app.kubernetes.io/version]"]) is True


def test_no_allowlist_flagged():
    # Default KSM deployment — emits zero kube_pod_labels → feature inert.
    assert _ok([]) is False
    assert _ok(["--telemetry-port=8081"]) is False


def test_partial_misconfig_flagged():
    # Gemini Pass-4: allowlist set to a DIFFERENT label only. KSM emits
    # kube_pod_labels (so the runtime sentinel stays silent) but WITHOUT the
    # version key → feature inert. The static lint MUST catch this.
    assert _ok(["--metric-labels-allowlist=pods=[app.kubernetes.io/managed-by]"]) is False


def test_allowlist_without_pods_entry_flagged():
    assert _ok(["--metric-labels-allowlist=nodes=[kubernetes.io/role]"]) is False


def test_live_repo_ksm_deployment_compliant():
    repo = lint._repo_root()
    tree = lint.read_k8s_tree(repo)
    targets = [p for p, docs in tree.items() if lint.ksm_containers(docs)]
    assert targets, "expected at least one KSM Deployment under k8s/ in kubectl's reading"
    for path in targets:
        assert lint.check_file(path) == [], f"{path} KSM deployment missing version allowlist"


# ── #3426: which files is the readers' answer, never the raw text ─────────────

_KSM_OK = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: kube-state-metrics}
spec:
  template:
    spec:
      containers:
      - name: ksm
        image: registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0
        args: ["--metric-labels-allowlist=pods=[app.kubernetes.io/version]"]
"""


def _tree(tmp_path, monkeypatch, files):
    for rel, text in files.items():
        p = tmp_path / "k8s" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_ksm_version_allowlist.py", "--ci"])


def test_ksm_only_in_a_comment_is_no_ksm_deployment(tmp_path, monkeypatch, capsys):
    # The raw text has both `kube-state-metrics` and `kind: Deployment`; the
    # old text preselection took the file, read no KSM Deployment from it,
    # and reported "All 1 ... allowlist" at rc 0.
    _tree(tmp_path, monkeypatch, {"03/ksm.yaml": (
        "# kind: Deployment of kube-state-metrics was removed\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")})
    assert lint.main() == lint.EXIT_CALLER_ERROR
    captured = capsys.readouterr()
    assert "no kube-state-metrics Deployment under k8s/" in captured.err
    assert "✅" not in captured.out


def test_a_file_kubectl_refuses_is_named(tmp_path, monkeypatch, capsys):
    # A compliant KSM Deployment elsewhere does not make the unread files pass.
    _tree(tmp_path, monkeypatch, {
        "03/deployment-ksm.yaml": _KSM_OK,
        "03/list.yaml": "apiVersion: v1\nkind: List\nitems: []\n",
        "03/no-kind.yaml": "replicas: 1\n",
    })
    assert lint.main() == lint.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "2 file(s) under k8s/ that kubectl's reading refuses" in err
    assert "k8s/03/list.yaml: " in err and "a List document is not read" in err
    assert "k8s/03/no-kind.yaml: " in err and "it has no `kind`" in err


def test_ksm_deployment_found_by_its_reading(tmp_path, monkeypatch, capsys):
    # Control: a tree with only the compliant Deployment passes — found by
    # its reading though no line spells `kind: Deployment`.
    _tree(tmp_path, monkeypatch, {"03/d.yaml": _KSM_OK.replace(
        "kind: Deployment", 'kind: "Deployment"')})
    assert lint.main() == lint.EXIT_OK
    assert "All 1 kube-state-metrics Deployment(s)" in capsys.readouterr().out
