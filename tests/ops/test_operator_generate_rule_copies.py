"""operator-generate 產出的 PrometheusRule 就是唯一的那份副本（#3427）。

repo 曾提交一份 `operator-manifests/`（每個 rule pack 一份 PrometheusRule），
由 `check_rulepack_sync.py` 只比 rule 內容。生成器後來開始帶
`release: kube-prometheus-stack` selector label，提交的那份沒跟上，閘門照樣
綠——因為它比的是「內容」，而落後的是 metadata。

修法是讓那份不存在：CRD 一律由使用者按需生成（`da-tools operator-generate
--components rules --output-dir <dir>`）。原本那道比對換成本檔，直接跑生成器、
對它**寫出來的檔**斷言：

* 每個 rule pack 恰好一份 PrometheusRule，檔名／物件名是宣告給的
  （`_lib_consumer_read.OPERATOR_RULES`），沒有多、沒有少；
* 每份的 rule 內容與來源 pack 語意相等（沿用 `check_rulepack_sync` 的
  `_extract` / `_diff_maps` 正規化，與 ConfigMap 副本受同一把尺）；
* metadata labels 帶齊兩個 selector label。⛔ 期望值寫成字面值、不從
  `PROMETHEUS_RULE_SELECTOR_LABELS` 取：從常數取的話，把 `release` 從常數拿掉
  時期望值會跟著變，這支測試就對 #3427 的缺陷形狀盲了。

另外兩條：

* 其他會產 PrometheusRule 的工具（`migrate-to-operator`、`rule-pack-split`）
  也帶同一組 selector label——#3427 點名 migrate 只帶 `prometheus`；
* 那份提交的副本不能悄悄回來：repo 裡追蹤的 YAML 不得有頂層
  `kind: PrometheusRule` 文件，只有 `testdata/` 與 `tests/` 底下的 fixture 例外。
  ⚠️ 量的是頂層（第 0 欄）`kind:`；包在 `List` 的 `items:` 裡、或由 Helm 樣板
  組出來的不在範圍——擋的是「把生成結果 commit 回來」這個形狀，不是所有寫法。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import _lib_consumer_read as cr
import check_rulepack_sync as sync
import generate_rule_pack_split as grps
import migrate_to_operator as mto

REPO = Path(__file__).resolve().parents[2]
_GENERATOR = REPO / "scripts" / "tools" / "ops" / "operator_generate.py"

#: 字面值，見模組 docstring。
_SELECTOR_LABELS = {"prometheus": "kube-prometheus", "release": "kube-prometheus-stack"}


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> Path:
    """以使用者會跑的那條指令生成 rule 副本，回傳 `--output-dir`。"""
    out = tmp_path_factory.mktemp("operator-crds")
    proc = subprocess.run(
        [sys.executable, str(_GENERATOR), "--components", "rules", "--gitops",
         "--rule-packs-dir", str(REPO / cr.RULE_PACKS.directory),
         "--output-dir", str(out)],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-1500:]
    return out


def _packs() -> "list[Path]":
    packs = cr.RULE_PACKS.files_under(REPO)
    assert packs, "no rule packs declared — the measurement itself is empty"
    return packs


def test_one_prometheus_rule_per_rule_pack(generated):
    expected = {cr.OPERATOR_RULES.file_name(cr.RULE_PACKS.member(p)) for p in _packs()}
    written = {p.name for p in generated.iterdir()}
    assert written == expected, (
        f"missing: {sorted(expected - written)}; extra: {sorted(written - expected)}")
    for name in sorted(written):
        docs = [d for d in yaml.safe_load_all((generated / name).read_text(encoding="utf-8"))
                if d is not None]
        assert len(docs) == 1 and docs[0]["kind"] == "PrometheusRule", name
        assert docs[0]["metadata"]["name"] == name.rsplit(".", 1)[0], name


def test_rule_content_equals_the_source_pack(generated):
    """同一把尺：source 以 Prometheus rule loader 的讀法讀、CRD 以 kubectl
    讀 manifest 再交給 rule loader（#3206），再經 `_extract` 正規化比對。"""
    packs = _packs()
    copies = [generated / cr.OPERATOR_RULES.file_name(cr.RULE_PACKS.member(p)) for p in packs]
    sources = cr.prometheus_rule_files(packs)
    manifests = cr.require_read(cr.manifest_rule_file_results(copies))
    drift, compared = {}, 0
    for pack, src, copy in zip(packs, sources, copies):
        groups = [g for m in manifests
                  if m.manifest == str(copy) and m.kind == "PrometheusRule" for g in m.groups]
        canonical = sync._extract(src.groups)
        assert canonical, f"{pack.name}: the source pack yielded no rule"
        compared += len(canonical)
        findings = sync._diff_maps(canonical, sync._extract(groups))
        if findings:
            drift[pack.name] = findings
    assert not drift, drift
    assert compared > 100, f"only {compared} rules compared"


def test_every_prometheus_rule_carries_both_selector_labels(generated):
    for f in sorted(generated.iterdir()):
        labels = yaml.safe_load(f.read_text(encoding="utf-8"))["metadata"]["labels"]
        missing = {k: v for k, v in _SELECTOR_LABELS.items() if labels.get(k) != v}
        assert not missing, f"{f.name}: lacks {missing} (has {labels})"


@pytest.mark.parametrize("emit", [
    lambda: mto.convert_rules_to_crd([], "x", "monitoring"),
    lambda: grps.to_prometheus_rule_crd([], "rule-pack-x.yaml", namespace="monitoring"),
], ids=["migrate-to-operator", "rule-pack-split"])
def test_other_prometheus_rule_emitters_carry_the_same_selector_labels(emit):
    labels = emit()["metadata"]["labels"]
    assert {k: labels.get(k) for k in _SELECTOR_LABELS} == _SELECTOR_LABELS, labels


# --- 提交的副本不能回來 -------------------------------------------------------

_TOP_LEVEL_PROMETHEUS_RULE = re.compile(r"""^kind[ \t]*:[ \t]*["']?PrometheusRule["']?[ \t]*(#.*)?$""",
                                        re.M)
_FIXTURE_PARTS = frozenset({"testdata", "tests"})


def committed_prometheus_rules(paths_and_texts) -> "list[str]":
    """*paths_and_texts* 中，帶頂層 PrometheusRule 文件、又不在 fixture 目錄下的路徑。"""
    return [rel for rel, text in paths_and_texts
            if _TOP_LEVEL_PROMETHEUS_RULE.search(text)
            and not _FIXTURE_PARTS.intersection(rel.split("/")[:-1])]


def _tracked_yaml():
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "*.yaml", "*.yml"],
        capture_output=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    rels = [r for r in proc.stdout.decode("utf-8").split("\0") if r]
    assert len(rels) > 100, "git ls-files listed almost nothing — the walk is broken"
    for rel in rels:
        path = REPO / rel
        if path.is_file():
            yield rel, path.read_text(encoding="utf-8", errors="replace")


def test_no_prometheus_rule_is_committed_outside_fixtures():
    # Tracked files only: an untracked local operator-manifests/ (generated by
    # hand, or left behind by a pull) commits nothing and must not turn this red.
    tracked = subprocess.run(
        ["git", "ls-files", "--", "operator-manifests"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    ).stdout.split()
    assert not tracked, (
        "operator-manifests/ is back — #3427 removed the committed copy; generate "
        "it with `da-tools operator-generate --components rules --output-dir <dir>`")
    found = committed_prometheus_rules(_tracked_yaml())
    assert not found, (
        "committed PrometheusRule manifests (#3427: generated on demand, never "
        f"committed): {found}")


def test_the_guard_sees_a_committed_copy_and_passes_over_fixtures():
    doc = "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata: {}\n"
    assert committed_prometheus_rules([("operator-manifests/da-rule-pack-a.yaml", doc)])
    assert committed_prometheus_rules([("x.yml", "---\nkind: 'PrometheusRule'  # c\n")])
    # Whitespace before the colon is still the top-level `kind` key to YAML.
    assert committed_prometheus_rules([("z.yaml", "kind : PrometheusRule\n")])
    assert not committed_prometheus_rules([("a/testdata/p.yaml", doc), ("tests/p.yaml", doc)])
    assert not committed_prometheus_rules([("k8s/x.yaml", "kind: ConfigMap\n"),
                                           ("k8s/y.yaml", "  - kind: PrometheusRule\n")])
