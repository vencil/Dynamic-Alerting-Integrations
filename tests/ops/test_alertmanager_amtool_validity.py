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

# Pin the amtool image to the version the cluster actually runs
# (k8s/03-monitoring/deployment-alertmanager.yaml) so the guard validates against
# the same parser production uses.
_AM_IMAGE = "prom/alertmanager:v0.32.0"


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
        offline dev box can't pull it — a missing image is not a config defect."""
        if subprocess.run(["docker", "image", "inspect", _AM_IMAGE],
                          capture_output=True, timeout=30).returncode == 0:
            return
        pull = subprocess.run(["docker", "pull", _AM_IMAGE],
                              capture_output=True, text=True, timeout=300)
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
