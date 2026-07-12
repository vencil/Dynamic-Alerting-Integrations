#!/usr/bin/env python3
"""test_alertmanager_amtool_validity.py — 守住「Alertmanager 自家 parser 拒收
generated config」這整類失敗。

背景：generate_alertmanager_routes.py 既有的 Python 測試只把組裝後的 config
當成 dict 驗結構，從不交給 Alertmanager 真正的 parser。結果 email receiver 路徑
曾有兩個 latent 缺陷，amtool 一驗就現形、Python dict 測試卻全綠：

  (A) email_configs[].to 被輸出成 YAML **list**，但 AM 的 `to` 是 **string**
      → `cannot unmarshal !!seq into string`。
  (B) email receiver 只有 smarthost、沒有 `from`，且 base config 無 global
      smtp_* → `no global SMTP from set`。

本檔兩層防線：
  1. TestEmailToCoercion — 純 Python，鎖住 (A) 的 builder 端 list→comma-string
     coercion 與 (B) 在 live _defaults.yaml 的 `from`（不需 docker，永遠跑）。
  2. TestAmtoolCheckConfig — 把 live conf.d + 部署用 base 依 GitOps
     --output-configmap 路徑 render 出來，丟給 prom/alertmanager 的 amtool
     check-config 當權威驗證（需 docker，缺則 skip）。
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

from generate_alertmanager_routes import (
    assemble_configmap,
    build_receiver_config,
    generate_inhibit_rules,
    generate_routes,
    load_base_config,
    load_tenant_configs,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONF_D = os.path.join(REPO_ROOT, "components", "threshold-exporter", "config", "conf.d")
_BASE_CM = os.path.join(REPO_ROOT, "k8s", "03-monitoring", "configmap-alertmanager.yaml")
_AM_DEPLOYMENT = os.path.join(REPO_ROOT, "k8s", "03-monitoring", "deployment-alertmanager.yaml")


def _deployed_am_image():
    """The amtool image, DERIVED from the deployment manifest (the SSOT) rather
    than hardcoded — so a cluster Alertmanager version bump can't leave this
    guard silently validating against a stale parser (false SUCCESS, prod crash).
    Matches `prom/alertmanager:<tag>` specifically, so the configmap-reload
    sidecar image is ignored."""
    text = open(_AM_DEPLOYMENT, encoding="utf-8").read()
    m = re.search(r'prom/alertmanager:[^\s"\']+', text)
    assert m, f"no prom/alertmanager image found in {_AM_DEPLOYMENT}"
    return m.group(0)


# Pin the amtool image to whatever the cluster actually runs (read from the
# deployment manifest) so the guard validates against the same parser production
# uses, and stays aligned automatically on a version bump.
_AM_IMAGE = _deployed_am_image()


def _render_live_alertmanager_yml(tmp_path):
    """Render alertmanager.yml exactly as `--output-configmap` does, from the
    committed conf.d + the deployed base ConfigMap. Returns the inner
    alertmanager.yml string (the thing Alertmanager actually loads)."""
    base_doc = yaml.safe_load(open(_BASE_CM, encoding="utf-8"))
    base_file = tmp_path / "base.yml"
    base_file.write_text(base_doc["data"]["alertmanager.yml"], encoding="utf-8")

    routing_configs, dedup_configs, _sw, enforced_routing, _mc = load_tenant_configs(_CONF_D)
    routes, receivers, _rw = generate_routes(
        routing_configs, allowed_domains=None, enforced_routing=enforced_routing)
    inhibit_rules, _dw = generate_inhibit_rules(dedup_configs)
    base = load_base_config(str(base_file))
    cm_yaml = assemble_configmap(base, routes, receivers, inhibit_rules)
    return yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"]


# ============================================================
# (A)/(B) — pure Python (no docker)
# ============================================================
class TestEmailToCoercion:
    """build_receiver_config() email `to` 形態 coercion（缺陷 A）。"""

    def test_list_to_joined_to_string(self):
        """多收件人 list `to` → AM 慣例的逗號分隔字串。"""
        cfg, warnings = build_receiver_config(
            {"type": "email", "to": ["a@example.com", "b@example.com"],
             "smarthost": "smtp.example.com:587", "from": "x@example.com"}, "db-a")
        assert warnings == []
        entry = cfg["email_configs"][0]
        assert entry["to"] == "a@example.com, b@example.com"
        assert isinstance(entry["to"], str)

    def test_single_element_list_to_string(self):
        """單一收件人 list `to` → 不帶逗號的字串。"""
        cfg, _ = build_receiver_config(
            {"type": "email", "to": ["solo@example.com"],
             "smarthost": "smtp.example.com:587", "from": "x@example.com"}, "db-a")
        assert cfg["email_configs"][0]["to"] == "solo@example.com"

    def test_string_to_passthrough(self):
        """已是字串的 `to` 原樣保留（不被誤包裝）。"""
        cfg, _ = build_receiver_config(
            {"type": "email", "to": "admin@example.com",
             "smarthost": "smtp.example.com:587", "from": "x@example.com"}, "db-a")
        assert cfg["email_configs"][0]["to"] == "admin@example.com"

    def test_email_without_from_is_skipped_with_warning(self):
        """缺陷 B 的 rule 化（防 latent 再生）：email receiver 缺 `from` 時，
        builder 直接 WARN+skip(回傳 None) — 不再 render 出 amtool 拒收的 config。
        部署 base 無 global smtp_*，故 `from` 為 per-receiver required（同 smarthost）。"""
        cfg, warnings = build_receiver_config(
            {"type": "email", "to": "ops@example.com",
             "smarthost": "smtp.example.com:587"}, "db-a")  # no `from`
        assert cfg is None
        assert any("requires 'from'" in w for w in warnings)

    def test_live_default_email_receiver_renders_string_to_and_from(self):
        """live _defaults.yaml 的 email default 建出的 receiver：`to` 為字串
        且帶 `from`（缺陷 A+B 的廉價結構回歸；權威驗證見 amtool 測試）。"""
        routing_configs, _d, _sw, _er, _mc = load_tenant_configs(_CONF_D)
        # db-a inherits the email default (db-b overrides to webhook).
        assert routing_configs["db-a"]["receiver"]["type"] == "email"
        cfg, warnings = build_receiver_config(routing_configs["db-a"]["receiver"], "db-a")
        assert warnings == []
        entry = cfg["email_configs"][0]
        assert isinstance(entry["to"], str), "email `to` must render as a string"
        assert entry.get("from"), "email receiver needs a `from` (no global smtp_from in base)"


# ============================================================
# #1092 0-pre — custom subtree per-tenant delivery (pure Python, no docker)
# ============================================================
def test_live_render_custom_route_carries_tenant_children(tmp_path):
    """live render（--output-configmap 路徑）的 custom isolation route 必須帶
    per-tenant children、每個 child 指向「已定義」的 tenant receiver（#1092
    0-pre 顯式契約化）。live conf.d 因 _routing_defaults 繼承，所有租戶必有
    tenant route → children 非空。"""
    am = yaml.safe_load(_render_live_alertmanager_yml(tmp_path))
    routes = am["route"]["routes"]
    customs = [r for r in routes if 'component="custom"' in r.get("matchers", [])]
    assert len(customs) == 1
    custom = customs[0]
    children = custom.get("routes", [])
    assert children, "custom isolation route must carry per-tenant children"

    # children mirror the live routing_configs tenant set, sorted, and each
    # points at its own tenant-<name> receiver
    routing_configs, _d, _sw, _er, _mc = load_tenant_configs(_CONF_D)
    expected = sorted(routing_configs.keys())
    child_tenants = []
    for child in children:
        hits = [m for m in (re.match(r'^tenant="(.+)"$', x)
                            for x in child["matchers"]) if m]
        assert len(hits) == 1, child
        tenant = hits[0].group(1)
        assert child["receiver"] == f"tenant-{tenant}"
        child_tenants.append(tenant)
    assert child_tenants == expected

    # route → DEFINED receiver contract (else amtool rejects the raw file)
    receiver_names = {r["name"] for r in am["receivers"]}
    assert {c["receiver"] for c in children} <= receiver_names

    # parent fallback + isolation semantics unchanged
    assert custom["receiver"] == "custom-alerts-firehose"
    assert custom["continue"] is False


# ============================================================
# amtool check-config — authoritative guard (docker)
# ============================================================
def _docker_available():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        # Expected "docker unavailable" failure modes only (binary gone after the
        # which() check, daemon down, timeout). Anything else surfaces rather than
        # silently skipping the guard — a silently-rotted gate is the #824 trap.
        return False


_DOCKER = _docker_available()


@pytest.mark.skipif(
    not _DOCKER,
    reason="docker not available — amtool check-config guard needs the "
           "prom/alertmanager image")
class TestAmtoolCheckConfig:
    """把 render 出來的 config 交給 Alertmanager 自家 parser 驗證。"""

    def _ensure_image(self):
        """Make sure the pinned image is present; skip (don't fail) when an
        offline dev box can't pull it — a missing image is not a config defect.
        Mirrors _docker_available's exception policy: a docker hiccup (timeout /
        OSError) on inspect-or-pull is an environmental skip, not a red test."""
        try:
            inspect = subprocess.run(["docker", "image", "inspect", _AM_IMAGE],
                                     capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pytest.skip(f"cannot inspect {_AM_IMAGE} (docker unavailable/offline)")
        if inspect.returncode == 0:
            return
        try:
            pull = subprocess.run(["docker", "pull", _AM_IMAGE],
                                  capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            pytest.skip(f"cannot obtain {_AM_IMAGE} (docker unavailable/offline)")
        if pull.returncode != 0:
            pytest.skip(f"cannot obtain {_AM_IMAGE} (offline?): {pull.stderr.strip()}")

    def _amtool_check(self, am_yml, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "alertmanager.yml").write_text(am_yml, encoding="utf-8")
        return subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "amtool",
             "-v", f"{etc.as_posix()}:/etc/alertmanager",
             _AM_IMAGE, "check-config", "/etc/alertmanager/alertmanager.yml"],
            capture_output=True, text=True, timeout=300)

    def test_live_config_is_amtool_valid(self, tmp_path):
        """live conf.d → render → amtool check-config 必須 SUCCESS。"""
        self._ensure_image()
        am_yml = _render_live_alertmanager_yml(tmp_path)
        r = self._amtool_check(am_yml, tmp_path)
        assert r.returncode == 0, (
            f"amtool rejected the rendered live config:\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
        assert "SUCCESS" in r.stdout

    def test_guard_catches_seq_to_regression(self, tmp_path):
        """守住守門員：amtool 確實會拒收 list 形態的 `to`（證明若 (A) 的
        coercion 退化，本 guard 會抓到，而非靜默放行）。"""
        self._ensure_image()
        bad = {
            "global": {"resolve_timeout": "5m"},
            "route": {"receiver": "x"},
            "receivers": [{
                "name": "x",
                "email_configs": [{
                    "to": ["a@example.com"],          # raw YAML seq — the (A) bug
                    "smarthost": "smtp.example.com:587",
                    "from": "alerting@example.com",
                }],
            }],
        }
        r = self._amtool_check(yaml.dump(bad), tmp_path)
        assert r.returncode != 0
        assert "unmarshal !!seq into string" in (r.stdout + r.stderr)


def test_am_image_tracks_deployment():
    """The guard's amtool image is DERIVED from the deployment manifest, not a
    hardcoded literal — so a cluster version bump can't leave it validating
    against a stale parser (Gemini drift review). No docker needed."""
    assert _AM_IMAGE.startswith("prom/alertmanager:")
    assert _AM_IMAGE in open(_AM_DEPLOYMENT, encoding="utf-8").read()
