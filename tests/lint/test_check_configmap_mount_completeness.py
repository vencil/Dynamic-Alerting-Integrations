"""check_configmap_mount_completeness.py：manifest 的每個 document 都由 kubectl 的讀法判定（#3426）。

這支 lint 讀 `configmap-rules-*.yaml` 與 Prometheus Deployment，原本對「不是 mapping」的
document 一律 `continue`：一份 manifest 裡被寫成 list 或沒有 `kind` 的 document，lint
不讀也不說，最後照樣印全數掛載、rc 0。現在 da-crdecode 的 manifest 讀法保證每個
document 是帶非空字串 `kind` 的物件（List 換成它的 items），讀不了就拒收；lint 把拒收以 rc 2
點名，不再有略過的路徑。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_configmap_mount_completeness as lint  # noqa: E402

_PACK = """\
apiVersion: v1
kind: ConfigMap
metadata: {name: prometheus-rules-demo}
data:
  demo.yml: |
    groups: []
"""

_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: prometheus}
spec:
  template:
    spec:
      volumes:
      - name: rules
        projected:
          sources:
          - configMap:
              name: prometheus-rules-demo
              items:
              - {key: demo.yml, path: demo.yml}
"""


def _tree(tmp_path: Path, pack: str, deployment: str) -> Path:
    d = tmp_path / "03-monitoring"
    d.mkdir()
    (d / "configmap-rules-demo.yaml").write_text(pack, encoding="utf-8")
    (d / "deployment-prometheus.yaml").write_text(deployment, encoding="utf-8")
    return d


def test_clean_tree_passes(tmp_path, capsys):
    d = _tree(tmp_path, _PACK, _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_OK
    assert "1 configmap-rules-* pack(s) all mounted" in capsys.readouterr().out


def test_a_pack_document_that_is_no_object_is_named(tmp_path, capsys):
    # A second document written as a list: it used to be skipped and the
    # run printed "all mounted" at rc 0.
    d = _tree(tmp_path, _PACK + "---\n- not: an object\n", _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "configmap-rules-demo.yaml" in err
    assert "the document starting at line 8: it is not a Kubernetes object: " \
           "the document is array" in err, err


def test_a_deployment_document_without_kind_is_named(tmp_path, capsys):
    d = _tree(tmp_path, _PACK, "replicas: 1\n---\n" + _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "the document starting at line 1: it is not a Kubernetes object: it has no `kind`" \
        in err, err


def test_a_pack_inside_a_list_is_read(tmp_path, capsys):
    # kubectl applies a `kind: List`'s items, and so are they read here: the
    # ConfigMap inside is a pack, and it is not mounted.
    listed = ("apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n"
              "  metadata: {name: prometheus-rules-other}\n  data: {x.yml: ''}\n")
    d = _tree(tmp_path, _PACK + "---\n" + listed, _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_VIOLATION
    assert "ConfigMap 'prometheus-rules-other'" in capsys.readouterr().err


def test_a_list_item_kubectl_would_not_apply_is_named(tmp_path, capsys):
    listed = "apiVersion: v1\nkind: List\nitems:\n- {apiVersion: v1, kind: List, items: []}\n"
    d = _tree(tmp_path, _PACK + "---\n" + listed, _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_CALLER_ERROR
    assert "a list inside a list is not read" in capsys.readouterr().err


def test_a_configmap_with_no_name_is_named(tmp_path, capsys):
    # #3426: it used to be dropped (no name, no pack) and the run passed.
    d = _tree(tmp_path, _PACK + "---\napiVersion: v1\nkind: ConfigMap\ndata: {x.yml: ''}\n",
              _DEPLOYMENT)
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_CALLER_ERROR
    assert "(object 2): a ConfigMap with no string metadata.name" in capsys.readouterr().err


def test_a_configmap_whose_data_is_no_mapping_is_named(tmp_path, capsys):
    # #3426: it used to be a pack with no keys — mounted whole, so it passed.
    d = _tree(tmp_path, _PACK.replace("data:\n  demo.yml: |\n    groups: []\n", "data: [x]\n"),
              _DEPLOYMENT.replace("              items:\n"
                                  "              - {key: demo.yml, path: demo.yml}\n", ""))
    assert lint.main(["--monitor-dir", str(d)]) == lint.EXIT_CALLER_ERROR
    assert ("(object 1): ConfigMap prometheus-rules-demo: its data is list, not a mapping"
            in capsys.readouterr().err)
